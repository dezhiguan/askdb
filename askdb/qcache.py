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
from typing import Any

_log = logging.getLogger("askdb.qcache")

#: 全 key 的版本前缀。应答 JSON 的字段形状一旦变化，把它 +1 即可让旧缓存
#: 整体失效，无需逐条清理（沿用项目里"改结果形状必升缓存 key"的惯例）。
_KEY_VERSION = "qc:v1"


def make_key(*, question: str, source_id: str, org_id: int, role: str) -> str:
    """把"决定结果的全部维度"折成一个 Redis key。

    question 用调用方 strip 过的原文精确串，**不做**大小写/空白规范化 ——
    规范化会把语义不同的两句问话并到同一条缓存上。
    """
    raw = "\x00".join([
        _KEY_VERSION,
        question,
        source_id or "builtin",
        str(org_id),
        role or "ANON",
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

    def stats(self) -> dict[str, Any]:
        return {"enabled": True, "hits": self._hits, "misses": self._misses,
                "ttl": self.ttl}


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
