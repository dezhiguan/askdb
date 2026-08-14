"""每日模型调用配额。

与旧实现的两点根本差别：

1. **计数发生在模型调用端，不是请求入口。**
   一次提问可能触发多次模型调用 —— 多步规划每步一次生成、每步一次结果评估、
   反思重试再各来一次。按请求计数，一个三步查询只算 1 次，而它实际花掉的是
   6~8 次的钱。配额是拿来兜成本的，就必须数在花钱的地方。

2. **计数存 Redis，不存本地文件。**
   本地文件的计数是每个进程各数各的：部署 3 个副本，实际上限就是 3 倍。
   这不是"略有偏差"，是配额直接失效。要多副本，计数就必须是共享的。

没配 Redis 时退回本地文件计数 —— 本机开发只有一个进程，文件计数是对的，
不必为了本地跑一下就先起个 Redis。

配额扣减是**预扣**：先原子地占掉一个名额，再发起模型调用。反过来
（先调用后计数）在并发下会超发 —— N 个请求同时读到 used=limit-1，
然后一起放行。Redis 侧用 Lua 保证"读-判-增"三步不可分割。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class QuotaExceeded(RuntimeError):
    """当日配额已用尽。带上用量便于直接展示给调用方。"""

    def __init__(self, used: int, limit: int, detail: str = ""):
        self.used = used
        self.limit = limit
        msg = f"已达当日模型调用上限（{used}/{limit}）"
        if detail:
            msg = f"{msg}：{detail}"
        super().__init__(msg)


class QuotaBackendError(RuntimeError):
    """计数后端不可用（Redis 连不上等）。"""


# 计数键的生存期。取 48 小时而非"到明天零点"：跨天的边界由键名里的日期
# 决定，TTL 只负责回收旧键，给足冗余比算得精确重要。
_TTL_SECONDS = 48 * 3600

# 预扣一个名额：读、判、增三步在 Redis 内一次执行完，并发下不会超发。
# 返回 -1 表示已达上限（此时不增），否则返回扣减后的用量。
_RESERVE_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if limit > 0 and used >= limit then
  return -1
end
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return n
"""


def _today() -> str:
    """按本地时区算"今天"。多副本部署在同一时区，口径一致。"""
    return datetime.now().astimezone().date().isoformat()


class _Backend:
    def peek(self) -> int:
        raise NotImplementedError

    def reserve(self, limit: int) -> int:
        """占用一个名额，返回占用后的用量；已达上限时抛 QuotaExceeded。"""
        raise NotImplementedError

    @property
    def kind(self) -> str:
        raise NotImplementedError


class NoQuota(_Backend):
    """不限量。limit <= 0 时使用 —— 连计数都不做，省掉一次 IO。"""

    def peek(self) -> int:
        return 0

    def reserve(self, limit: int) -> int:
        return 0

    @property
    def kind(self) -> str:
        return "none"


class FileQuota(_Backend):
    """本地文件计数，供单进程（本机开发）使用。

    用 flock 而不是"读-改-写"了事：uvicorn 起多 worker 时同机也有并发。
    但它挡不住跨机并发 —— 那是 Redis 后端的职责，别把 hostPath
    挂给多个副本然后指望这个文件能算对。
    """

    def __init__(self, path: Path):
        self.path = path

    def _read(self, f) -> int:
        f.seek(0)
        raw = f.read().strip()
        if not raw:
            return 0
        try:
            data = json.loads(raw)
        except ValueError:
            return 0
        if data.get("date") != _today():
            return 0
        return int(data.get("used", 0))

    def peek(self) -> int:
        if not self.path.exists():
            return 0
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return self._read(f)
        except OSError:
            return 0

    def reserve(self, limit: int) -> int:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    used = self._read(f)
                    if limit > 0 and used >= limit:
                        raise QuotaExceeded(used, limit)
                    used += 1
                    f.seek(0)
                    f.truncate()
                    f.write(json.dumps({"date": _today(), "used": used}))
                    f.flush()
                    os.fsync(f.fileno())
                    return used
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError as e:
            raise QuotaBackendError(f"配额文件不可写：{e}") from e

    @property
    def kind(self) -> str:
        return "file"


class RedisQuota(_Backend):
    """Redis 计数，供多副本部署使用。"""

    def __init__(self, url: str, prefix: str):
        self.url = url
        self.prefix = prefix
        self._client: Any = None

    def _conn(self):
        if self._client is not None:
            return self._client
        try:
            import redis  # 延迟导入：没配 Redis 的部署不必装这个包
        except ImportError as e:  # pragma: no cover - 依赖缺失属部署问题
            raise QuotaBackendError(
                "配置了 Redis 配额但未安装 redis 包，装 askdb[redis] 后重试"
            ) from e
        try:
            self._client = redis.Redis.from_url(
                self.url, socket_timeout=2, socket_connect_timeout=2,
                decode_responses=True,
            )
        except Exception as e:
            raise QuotaBackendError(f"Redis 连接参数无效：{e}") from e
        return self._client

    @property
    def _key(self) -> str:
        return f"{self.prefix}:{_today()}"

    def peek(self) -> int:
        try:
            v = self._conn().get(self._key)
        except Exception as e:
            raise QuotaBackendError(f"Redis 读取失败：{e}") from e
        return int(v or 0)

    def reserve(self, limit: int) -> int:
        try:
            n = self._conn().eval(_RESERVE_LUA, 1, self._key, limit, _TTL_SECONDS)
        except Exception as e:
            raise QuotaBackendError(f"Redis 写入失败：{e}") from e
        n = int(n)
        if n < 0:
            raise QuotaExceeded(limit, limit)
        return n

    @property
    def kind(self) -> str:
        return "redis"


class DailyQuota:
    """对外的统一入口：上限 + 后端 + 后端故障时的取舍。"""

    def __init__(self, limit: int, backend: _Backend, on_backend_error: str = "block"):
        self.limit = limit
        self.backend = backend
        # 计数后端挂了怎么办。默认 block：配额存在的意义就是兜住成本，
        # 后端一挂就无限放行，等于"最需要它的时候它不在"。
        # 直查 SQL 链路不调模型、不过配额，Redis 挂了它照常可用。
        self.on_backend_error = on_backend_error if on_backend_error in ("block", "allow") else "block"

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def kind(self) -> str:
        return self.backend.kind

    def peek(self) -> int:
        """只读用量，不占名额。后端异常时返回 0 —— 这只用于展示与快速失败，
        真正的把关在 reserve()，那里不会因为读失败就放行。"""
        if not self.enabled:
            return 0
        try:
            return self.backend.peek()
        except QuotaBackendError:
            return 0

    def exhausted(self) -> tuple[bool, int]:
        if not self.enabled:
            return False, 0
        used = self.peek()
        return used >= self.limit, used

    def reserve(self) -> int:
        if not self.enabled:
            return 0
        try:
            return self.backend.reserve(self.limit)
        except QuotaBackendError as e:
            if self.on_backend_error == "allow":
                return 0
            raise QuotaExceeded(-1, self.limit, str(e)) from e


# 后端按配置缓存复用：LlmClient 是每请求新建的，若每次都新建 Redis 客户端，
# 连接池就废了 —— 每个请求一条 TCP，压测时直接把连接数打满。
_CACHE: dict[tuple, DailyQuota] = {}


def build_quota(cfg) -> DailyQuota:
    """按配置装配配额器。

    选后端的规则只有一条：配了 Redis 就用 Redis，没配就用本地文件。
    不做"先试 Redis 连不上再退回文件"的自动降级 —— 那会让多副本部署在
    Redis 抖动时静默变成每副本各算各的，正是这个模块要解决的问题。
    """
    obs = cfg.raw.get("observability", {}) or {}
    q = obs.get("quota", {}) or {}
    limit = cfg.daily_quota

    url_env = str(q.get("redis_url_env") or "").strip()
    url = (os.environ.get(url_env) or "").strip() if url_env else ""
    prefix = str(q.get("key_prefix") or "askdb:quota")
    on_err = str(q.get("on_backend_error") or "block").lower()

    if limit <= 0:
        key = ("none",)
    elif url:
        key = ("redis", url, prefix, limit, on_err)
    else:
        key = ("file", str(cfg.audit_log), limit)

    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    backend: _Backend
    if limit <= 0:
        backend = NoQuota()
    elif url:
        backend = RedisQuota(url, prefix)
    else:
        # 与审计日志同目录、同实例名 —— public 与本机开发各算各的，
        # 不会因为共用一个计数文件而互相扣额度。
        p = cfg.audit_log
        backend = FileQuota(p.with_name(p.stem + "-quota.json"))

    dq = DailyQuota(limit, backend, on_err)
    _CACHE[key] = dq
    return dq


def reset_cache() -> None:
    """仅供测试：配置在用例之间会变，缓存不清会串。"""
    _CACHE.clear()
