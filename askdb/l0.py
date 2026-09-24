"""L0：进程内的派生件缓存。**只放确定性派生物，绝不放答案。**

为什么答案不能放在这里
----------------------
qcache 模块头已经把这条写死了：应答缓存必须跨副本共享，进程内缓存会各存各的、
命中不一致 —— 两个副本对同一句话给出不同的新鲜度，而页面上看不出区别。
所以 L1/L2/L3 一律落 Redis 或 PostgreSQL，这里一条答案都不存。

那这一层放什么
--------------
放**输入相同就必然输出相同**的东西，且重算它们除了 CPU 与一次网络往返之外
没有任何副作用：

  · 问题向量        —— 同一句话嵌出来的向量恒定，重嵌一次要付 300ms 与一次计费
  · 渲染好的系统提示 —— (工具规格, 旋钮档位) 固定即固定，每轮 decide 重拼一遍

这三类共同的性质是：**缓存失效的后果只是多算一次，不会答错。** 凡是"过期了会
让答案变味"的东西（表结构、脱敏配置、行数据）都不属于这一层 —— 它们的失效
要靠指纹，而指纹要跨副本一致，那是 L1 以上的事。

命中不记 token，也不记钱
------------------------
向量命中时这次真的没有调用厂商接口，所以 schema_recall 那一格的 tok_in 与
cost_cny 就是 0 —— 那不是账面漏了，是真没花。与 trace.embed_cost_cny 那句
"0 元在成本页上是显眼的"不矛盾：那条说的是**调用发生了却没记账**，这里是
**调用压根没发生**。两者在成本页上要能分开，所以 stats() 把命中数露出去。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

__all__ = ["memo", "stats", "reset"]

#: 每个 bucket 的容量与存活时间。**按 bucket 分开配**：向量条目小、值钱、
#: 可以多存久存；渲染好的提示词一份就是几千字符，存几十份足够（旋钮档位
#: 总共就那么几种组合），存多了是白占内存。
_BUCKETS: dict[str, tuple[int, float]] = {
    # bucket: (最多几条, 存活秒数)
    "embed":  (2048, 3600.0),
    "prompt": (64, 3600.0),
}
_DEFAULT = (256, 600.0)

_lock = threading.Lock()
_store: dict[str, "OrderedDict[str, tuple[float, Any]]"] = {}
_hits: dict[str, int] = {}
_misses: dict[str, int] = {}


def _bucket(name: str) -> "OrderedDict[str, tuple[float, Any]]":
    d = _store.get(name)
    if d is None:
        d = OrderedDict()
        _store[name] = d
    return d


def memo(bucket: str, key: str, produce: Callable[[], Any]) -> Any:
    """取缓存，没有就算一次并存下来。

    **produce 在锁外调用。** 它可能是一次网络往返（嵌入调用），握着锁去做
    会让所有副本内的请求排成一队 —— 那比多嵌一次贵得多。代价是并发的两个
    请求可能各算一次，而这一层的语义允许这件事（多算一次只是多花一次，
    不会答错）。真正不能重复的是模型调用，那一层有 Redis 的 singleflight。
    """
    cap, ttl = _BUCKETS.get(bucket, _DEFAULT)
    now = time.monotonic()
    with _lock:
        d = _bucket(bucket)
        hit = d.get(key)
        if hit is not None:
            ts, val = hit
            if now - ts <= ttl:
                d.move_to_end(key)
                _hits[bucket] = _hits.get(bucket, 0) + 1
                return val
            del d[key]
        _misses[bucket] = _misses.get(bucket, 0) + 1

    val = produce()

    with _lock:
        d = _bucket(bucket)
        d[key] = (time.monotonic(), val)
        d.move_to_end(key)
        while len(d) > cap:
            d.popitem(last=False)
    return val


def stats() -> dict[str, Any]:
    """给 /api/health 看的。**按 bucket 分开报** —— 合成一个总命中率之后，
    "向量缓存没生效"会被提示词那一栏的高命中率盖住，而两者是完全不同的两件事。
    """
    with _lock:
        return {
            b: {"size": len(_store.get(b, {})),
                "hits": _hits.get(b, 0), "misses": _misses.get(b, 0)}
            for b in sorted(set(_BUCKETS) | set(_store))
        }


def reset() -> None:
    """单测用。生产没有任何地方该调它 —— 这一层没有"需要手动清"的场景。"""
    with _lock:
        _store.clear()
        _hits.clear()
        _misses.clear()
