"""应答缓存：相同提问在 TTL 内直接返回上次的完整结果，不再调用模型。

与 quota.py 的关系与差别
------------------------
两者都以 Redis 为后端、都按配置装配、都对"没配 Redis"的本地开发退化。
但**失败取舍相反**，这是本模块最要紧的一条：

  · quota 是护栏 —— Redis 挂了要 **fail-closed**（拒绝模型调用），
    否则最需要兜成本时它不在。
  · cache 是优化 —— Redis 挂了必须 **fail-open**（当作未命中，照常走模型），
    否则一次缓存抖动就把整条查询链路打断，代价远大于收益。

因此这里所有 Redis 异常都被吞掉：读失败当 miss，写失败静默丢弃，各记一次
告警日志即可。缓存永远不该成为 /api/ask 返回 500 的原因。

为什么缓存的是「完整应答 JSON」而不是「生成的 SQL」
--------------------------------------------------
按 2026-09-08 决定：命中即"零模型、零配额、零执行"，直接把上次的结果原样
返回，TTL 60s 兜住陈旧。省掉的不只是 1.1s 的模型调用，还有那次调用要占的
每日配额与费用 —— 命中的提问对配额是完全免费的。

key 里为什么必须带 source / org / role
--------------------------------------
同一句问话，在不同数据源、不同租户、不同角色下是不同的结果（脱敏列、可见表
都可能不同）。key 少一维就是跨源/跨身份串结果。当前公开实例匿名统一（GUEST），
这几维会塌缩成同一个值，但 auth.required 翻转过、将来还会翻转，所以现在就把
它们全带上，而不是等出事再补。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

_log = logging.getLogger("askdb.qcache")

#: 全 key 的版本前缀。应答 JSON 的字段形状一旦变化，把它 +1 即可让旧缓存
#: 整体失效，无需逐条清理（沿用项目里"改结果形状必升缓存 key"的惯例）。
#: v2（2026-09-15）：key 里补进了配置指纹。旧 key 一律作废，理由见 scope()。
_KEY_VERSION = "qc:v2"

#: 单飞锁的存活时间。取的是"一条链路最长可能跑多久"的上界 —— 交接出去的
#: 长任务可以跑几分钟，但等在锁后面的人早就不等了（wait_ms 是秒级），
#: 锁活得久一点只影响"第二个人要不要也去跑一遍"，不影响任何正确性。
_LEAD_TTL_S = 120
_LEAD_POLL_S = 0.05


def scope(cfg) -> str:
    """把"这个答案在什么条件下才依然成立"折成一个指纹。

    2026-09-15 补的一维。此前 key 只有 (问题, 源, 组织, 角色)，于是
    **源的白名单、脱敏列、护栏参数改了之后 key 不变** —— 旧答案继续被返回
    最长一个 TTL。新鲜度只是其中一面，更要紧的是脱敏：把一列标成敏感之后，
    改之前那份没打码的结果还在缓存里，而改配置的人理所当然地认为改完即生效。

    指纹里放什么，判据是"它变了，同一句话的**正确答案**会不会变"：
      · 表白名单与列（含敏感标记）—— 变了，可见面与打码都变
      · guard 段（行数上限、扫描阈值、超时、SELECT * 开关）—— 变了，拦不拦变
      · 租户列与隔离模式 —— 变了，注入的过滤条件变
      · 方言 —— 变了，SQL 本身都不一样
    不放什么：模型与单价（换模型不改变"什么是对的"，只改变花多少钱），
    以及任何随请求变的东西（那是 key 的另外四维在管）。

    每次提问算一遍。几十张表拼串再 sha256 是几十微秒的事，而把它 memo 起来
    需要一个"配置变没变"的信号 —— 那正是这个函数自己要产出的东西。
    """
    tenant = (cfg.raw.get("tenant") or {})
    try:
        dialect = str(cfg.dialect)
    except Exception:          # noqa: BLE001 —— 没有默认源时它会抛，见 config._ds
        # 取不到方言不是错：调用方此刻可能还没选源。指纹照旧成立，
        # 少一维只会让它更保守（同一串在两种方言下撞不到一起，因为
        # 表白名单本来就不同）。
        dialect = ""
    parts: list[str] = [
        dialect, str(tenant.get("column", "")), str(tenant.get("mode", "")),
    ]
    guard = (cfg.raw.get("guard") or {})
    for k in sorted(guard):
        parts.append(f"{k}={guard[k]}")
    for name in sorted(getattr(cfg, "tables", {}) or {}):
        parts.append(name)
        cols = cfg.tables[name].columns or {}
        for cn in sorted(cols):
            parts.append(f"{cn}:{int(bool(cols[cn].sensitive))}")
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def make_key(*, question: str, source_id: str, org_id: int, role: str,
             scope_fp: str = "") -> str:
    """把"决定结果的全部维度"折成一个 Redis key。

    question 用调用方 strip 过的原文精确串，**不做**大小写/空白规范化 ——
    规范化会把语义不同的两句问话并到同一条缓存上。近义问法的收敛是
    语义缓存（semcache）的职责，不是这一层的。
    """
    raw = "\x00".join([
        _KEY_VERSION,
        question,
        source_id or "builtin",
        str(org_id),
        role or "ANON",
        scope_fp or "-",
    ])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest


class AnswerCache:
    """禁用态的空实现，同时充当接口定义。build 出来的可能就是它本身。"""

    enabled: bool = False

    def get(self, key: str) -> dict[str, Any] | None:
        return None

    def put(self, key: str, value: dict[str, Any], ttl: int) -> None:
        return None

    def lead(self, key: str, wait_ms: int = 0) -> dict[str, Any] | None:
        """单飞：未命中时决定"这次由谁去跑"。禁用态一律自己跑。"""
        return None

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "hits": 0, "misses": 0}


class RedisAnswerCache(AnswerCache):
    """Redis 后端，供多副本共享。所有 Redis 异常一律 fail-open。"""

    enabled = True

    def __init__(self, url: str, prefix: str, ttl: int):
        self.url = url
        self.prefix = prefix.rstrip(":")
        self.ttl = ttl
        self._client: Any = None
        # 进程内命中计数，仅供 /api/health 展示。多副本下它是本副本的局部视图，
        # 不追求跨副本精确 —— 缓存本身的正确性靠 Redis，不靠这个计数。
        self._hits = 0
        self._misses = 0
        #: 因为别人正在跑同一个问题而省掉的整条链路（见 lead）。
        self._followers = 0
        # Redis 连不上时只告警一次，别让每个请求都刷一行日志。
        self._warned = False

    def _conn(self):
        if self._client is not None:
            return self._client
        import redis  # 延迟导入：没装 redis 包的本地开发不会走到这里

        self._client = redis.Redis.from_url(
            self.url, socket_timeout=2, socket_connect_timeout=2,
            decode_responses=True,
        )
        return self._client

    def _k(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def _degrade(self, action: str, err: Exception) -> None:
        if not self._warned:
            _log.warning("应答缓存 Redis %s 失败，本次起按未命中处理（fail-open）：%s",
                         action, err)
            self._warned = True

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self._conn().get(self._k(key))
        except Exception as e:               # noqa: BLE001 —— 缓存必须 fail-open
            self._degrade("读取", e)
            return None
        if raw is None:
            self._misses += 1
            return None
        try:
            val = json.loads(raw)
        except (ValueError, TypeError) as e:
            # 存进去的是自己 dumps 的，正常不该坏；坏了就当没命中并顺手删掉。
            self._degrade("反序列化", e)
            try:
                self._conn().delete(self._k(key))
            except Exception:                # noqa: BLE001
                pass
            self._misses += 1
            return None
        self._hits += 1
        return val

    def put(self, key: str, value: dict[str, Any], ttl: int) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            self._degrade("序列化", e)
            return
        try:
            self._conn().set(self._k(key), payload, ex=max(1, int(ttl)))
        except Exception as e:               # noqa: BLE001 —— 写失败静默丢弃
            self._degrade("写入", e)

    def lead(self, key: str, wait_ms: int = 0) -> dict[str, Any] | None:
        """未命中之后、真去跑之前调一次。返回值的含义只有两种：

          · dict —— 别人刚跑完并写进来了，直接用它，**不要再跑一遍**
          · None —— 这次由你跑

        解决的是缓存击穿：站点上同一个问题常被很多人在同一秒点开，而 L1 是
        "跑完才写"，于是这些请求全部穿透、各跑一遍模型、各扣一次配额。
        一条 12 秒的链路被同时点 8 次，就是 8 倍的钱和 8 个后台槽位。

        **fail-open 到底**：抢不到锁又等不到结果，就返回 None 让调用方自己跑。
        最坏情况退化成改动前的行为（多跑几次），绝不会变成"等在这里"或报错。
        锁本身带 TTL，持锁者崩溃也只是让后来者多等一个 TTL 的零头。

        wait_ms=0 表示"不等"——只看一眼有没有人刚写完。默认不等是有意的：
        等待要占住一个 web 工作线程，而这条链路的中位耗时是秒级，
        等多久都可能是白等。调用方明确知道值得等时才传。
        """
        lock = f"{self._k(key)}:lead"
        try:
            got = self._conn().set(lock, "1", nx=True, ex=_LEAD_TTL_S)
        except Exception as e:                # noqa: BLE001 —— 锁也 fail-open
            self._degrade("抢锁", e)
            return None
        if got:
            return None                        # 我是首跑
        if wait_ms <= 0:
            return None                        # 不等：照常自己跑
        deadline = time.monotonic() + wait_ms / 1000.0
        while time.monotonic() < deadline:
            time.sleep(_LEAD_POLL_S)
            hit = self.get(key)
            if hit is not None:
                self._followers += 1
                return hit
        return None

    def release(self, key: str) -> None:
        """首跑结束（成功或失败）都要放锁 —— 失败不放的话，这个问题在锁的
        TTL 内会被后来者当成"有人在跑"，而其实没有人在跑。"""
        try:
            self._conn().delete(f"{self._k(key)}:lead")
        except Exception as e:                # noqa: BLE001
            self._degrade("放锁", e)

    def stats(self) -> dict[str, Any]:
        return {"enabled": True, "hits": self._hits, "misses": self._misses,
                "followers": self._followers, "ttl": self.ttl}


#: 与 quota._CACHE 同理：同一份配置反复 build 不必每次新建连接池。
_CACHE: dict[tuple, AnswerCache] = {}


def build_answer_cache(cfg) -> AnswerCache:
    """按配置装配应答缓存。

    规则：answer_cache.enabled 为真、且能解析出 Redis URL，才返回真缓存；
    否则一律返回禁用态空实现（本地开发、或未配 Redis 的部署）。

    Redis URL 的来源：优先 answer_cache.redis_url_env，缺省则复用配额那一个
    （observability.quota.redis_url_env）—— 两者本就该指同一个 Redis 实例。
    """
    ac = cfg.raw.get("answer_cache", {}) or {}
    if not ac.get("enabled", False):
        return AnswerCache()

    ttl = int(ac.get("ttl_seconds", 60) or 60)
    prefix = str(ac.get("key_prefix") or "askdb:qcache")

    url_env = str(ac.get("redis_url_env") or "").strip()
    if not url_env:
        q = (cfg.raw.get("observability", {}) or {}).get("quota", {}) or {}
        url_env = str(q.get("redis_url_env") or "").strip()
    url = (os.environ.get(url_env) or "").strip() if url_env else ""

    if not url or ttl <= 0:
        # 配了 enabled 却没有可用 Redis：退化为禁用，绝不退回进程内 dict
        # （多副本下进程内缓存会各存各的、命中不一致）。
        return AnswerCache()

    ck = (url, prefix, ttl)
    hit = _CACHE.get(ck)
    if hit is None:
        hit = RedisAnswerCache(url, prefix, ttl)
        _CACHE[ck] = hit
    return hit
