"""长任务交接：同步执行超过软阈值就把它交给后台，主请求先回执。

**一条执行只有一个交接判据**：从这次执行开始计时，墙钟越过 ``async_after_ms``
就交接。交接不是取消重跑 —— 已经花掉的步数与 token 全部留在检查点里，
变的只是"结果怎么送达"。

交接点不止入口
--------------
主请求线程在入口等那一下是**兜底**。图在每个节点边界也检查同一个截止时刻
（``Handoff.check``），谁先越线谁触发：

  · 入口那一路管得住"卡在某一步不动"的执行；
  · 节点边界那一路管得住"跑到一半才看出会长"（多步、token 过半、大扫描），
    并且对**不经过入口等待的执行路径**（续跑、自愈、审批代跑）是唯一交接点。

两条路都通过同一个 Handoff 对象，所以"什么时候算长"只有一个定义。

容量
----
后台池是**进程内**的，有界。两条边界，取舍相反，都是有意的：

  · 每账号在跑上限 —— 满了**拒绝**并说明理由，不排队。排队会让回执里那句
    "正在后台执行"变成谎话：任务其实在队列里一步没动。
  · 全池满 —— **不交接**，在请求线程里同步跑到底。宁可这一条慢，
    也不能因为没有后台槽位就把它丢掉。

与「中断恢复 / 任务中心」共用同一套审计机制：run_agent 进入即写 PHASE_STARTED、
完成写 final，所以交接出去的任务天然在任务中心可见、可凭 thread_id 追踪。
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .graph import AskResult

_log = logging.getLogger("askdb.async_runner")

#: 后台池大小与每账号在跑上限的默认值。两者都可由 agent.async_pool_size /
#: agent.async_per_user 覆盖 —— 部署形态不同，能同时挂多少条长任务也不同。
DEFAULT_POOL_SIZE = 16
DEFAULT_PER_USER = 3


class CapacityExceeded(Exception):
    """这个账号在后台跑的任务已经到上限。**拒绝，不排队**（见模块头注）。"""


@dataclass
class Handoff:
    """一次执行的交接现场。**不进检查点** —— 它描述的是"这次怎么送达"，
    不是"这次算到哪儿了"。

    deadline 是单调时钟上的绝对时刻，不是剩余毫秒：节点边界与入口等待器
    读的必须是同一个"什么时候算超时"，存相对量就会各算各的。
    """
    deadline: float
    #: 入口等待器挂在这上面。节点边界请求交接时把它叫醒，不必等满阈值。
    wake: threading.Event = field(default_factory=threading.Event)
    detached: bool = False
    reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def request(self, reason: str) -> None:
        """请求交接。**幂等**：第一个理由留下，后来的不覆盖 ——
        回执里那句话要说的是"因为什么交接的"，改写只会让它对不上审计。"""
        with self._lock:
            if not self.reason:
                self.reason = reason
        self.wake.set()

    def check(self, *, step: int = 0, tok_used: int = 0, cost_cap: int = 0,
              explain_rows: int = 0, scan_threshold: int = 0,
              multi_step: bool = False) -> None:
        """节点边界调这一次。越过截止时刻、或已经能断定"这条会长"就请求交接。

        提前交接的三条判据（A-5/A-6/A-7）都拿不到于入口，只能在这里判：
        多走一步就多占一秒连接，而它们的结论此刻已经确定。
        """
        if self.detached or self.reason:
            return
        if time.monotonic() >= self.deadline:
            self.request("执行超过阈值")
        elif multi_step or step >= 3:
            self.request(f"多步链路（已第 {step} 步）")
        elif cost_cap > 0 and tok_used >= cost_cap // 2:
            self.request(f"token 已用 {tok_used}/{cost_cap}")
        elif scan_threshold > 0 and explain_rows >= scan_threshold // 2:
            self.request(f"预估扫描 {explain_rows:,} 行")

    def mark_detached(self) -> None:
        with self._lock:
            self.detached = True


def new_handoff(threshold_ms: int) -> Handoff:
    """按阈值造一个交接现场。threshold_ms <= 0 表示立即交接。"""
    return Handoff(deadline=time.monotonic() + max(0, threshold_ms) / 1000.0)


class _Pool:
    """有界后台池 + 每账号在跑计数。

    自己数在跑的条数，不问 ThreadPoolExecutor —— 它的队列深度是私有属性，
    而且"排在队里"与"正在跑"对本模块是两件完全不同的事（见模块头注）。
    """

    def __init__(self, size: int = DEFAULT_POOL_SIZE):
        self.size = size
        self._pool = _cf.ThreadPoolExecutor(max_workers=size,
                                            thread_name_prefix="askdb-agent")
        self._lock = threading.Lock()
        self._inflight = 0
        self._by_user: dict[str, int] = {}

    def reserve(self, user: str, per_user: int) -> bool:
        """占一个槽位。占不到返回 False —— **调用方据此决定是拒绝还是同步跑**，
        两种情况的处置完全不同，所以这里不替它做决定。"""
        with self._lock:
            if self._inflight >= self.size:
                return False
            if user and per_user > 0 and self._by_user.get(user, 0) >= per_user:
                raise CapacityExceeded(user)
            self._inflight += 1
            if user:
                self._by_user[user] = self._by_user.get(user, 0) + 1
            return True

    def release(self, user: str) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if user and user in self._by_user:
                left = self._by_user[user] - 1
                if left > 0:
                    self._by_user[user] = left
                else:
                    del self._by_user[user]

    def submit(self, fn: Callable[[], Any], user: str) -> _cf.Future:
        fut = self._pool.submit(fn)
        fut.add_done_callback(lambda _f: self.release(user))
        return fut

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"size": self.size, "inflight": self._inflight,
                    "users": len(self._by_user)}


_POOL = _Pool()


def configure(*, pool_size: int = 0) -> None:
    """按配置定池子大小。**只在池空时换**：换掉正在服务的池等于把在跑的任务
    连同它们的 release 回调一起丢掉，而那些任务正是最不该丢的那批。"""
    global _POOL
    if pool_size <= 0 or pool_size == _POOL.size:
        return
    if _POOL.stats()["inflight"] > 0:
        _log.warning("后台池非空，本次不改容量：%s → %s", _POOL.size, pool_size)
        return
    _POOL = _Pool(pool_size)


def stats() -> dict[str, Any]:
    return _POOL.stats()


def run_or_detach(fn: Callable[[], AskResult], threshold_ms: int, thread_id: str,
                  *, user: str = "", per_user: int = DEFAULT_PER_USER,
                  handoff: Handoff | None = None,
                  on_detached_done: Callable[[AskResult], None] | None = None,
                  ) -> tuple[AskResult | None, dict[str, Any] | None]:
    """跑 fn，最多同步等到 handoff 的截止时刻。

    返回 ``(result, None)``  —— 阈值内跑完（或没有后台槽位、只能同步跑完）；
    返回 ``(None, notice)``  —— 已交接后台，notice 是给前端的回执。

    ``CapacityExceeded`` 会抛出来 —— 这个账号挂着的长任务太多了，
    调用方要把它翻成一句"你有 N 条任务还在跑"，而不是又开一条。
    """
    ho = handoff or new_handoff(threshold_ms)
    try:
        got_slot = _POOL.reserve(user, per_user)
    except CapacityExceeded:
        raise
    if not got_slot:
        # 全池满：同步跑到底。慢，但不丢 —— 交接不出去时，"跑完"永远比
        # "回执一个没人在执行的 thread_id"诚实。
        _log.warning("后台池已满（%s），本次同步执行：%s", _POOL.size, thread_id)
        return fn(), None

    def _job() -> AskResult:
        r = fn()
        # 交接出去的那些，把完整结果暂存一份供原地接管取回（fail-open，
        # 取不到时轮询端点退回按审计记录拼的结果块）。
        if ho.detached and on_detached_done is not None:
            try:
                on_detached_done(r)
            except Exception:         # noqa: BLE001
                pass                  # 暂存失败不该影响这次执行的结果
        return r

    if threshold_ms <= 0:
        # 立即交接（as_task / 凭票重跑）。**在提交之前就标**：标晚了，一个跑得
        # 飞快的任务会在标之前完成，于是既没同步返回、也没暂存结果，
        # 原地接管只能退回审计结果块。
        ho.mark_detached()
        ho.request("按请求立即转后台")
        _POOL.submit(_job, user)
        return None, _notice(thread_id, ho.reason)

    fut = _POOL.submit(_job, user)
    fut.add_done_callback(lambda _f: ho.wake.set())

    remaining = ho.deadline - time.monotonic()
    if remaining > 0:
        ho.wake.wait(timeout=remaining)
    if fut.done():
        return fut.result(), None
    # 不取消 future —— 让它在池里跑完并写 final 审计，任务中心据此更新。
    ho.mark_detached()
    return None, _notice(thread_id, ho.reason or "执行超过阈值")


def _notice(thread_id: str, reason: str) -> dict[str, Any]:
    return {
        "async": True,
        "thread_id": thread_id,
        "trace_id": thread_id,
        "reason": reason,
        "message": "该任务耗时较长，已转后台执行；结果就绪后会在此处直接显示，"
                   "也可到任务中心查看。",
    }
