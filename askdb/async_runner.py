"""长任务自动异步（可信数据 Agent v2 §2）。

判定短/长不靠一次预判，靠**墙钟软阈值兜底**：把 run_agent 提交到后台线程池，
主请求最多等 threshold_ms；等到就同步直返（短任务，绝大多数），等不到就转异步——
返回「耗时较长，请到任务中心查看」，而后台线程**继续跑到完成并写 final 审计**，
任务中心据审计把它从 RUNNING 显示到 SUCCEEDED。

异步与「中断恢复 / 任务中心」共用同一套审计机制：run_agent 进入即写 PHASE_STARTED，
完成写 final，所以转异步的任务天然在任务中心可见、可凭 thread_id 追踪。

Tier-1 预判（as_task / 多步 / 大扫描）走 threshold_ms=0，即"立即异步"。
"""
from __future__ import annotations

import concurrent.futures as _cf
from typing import Any, Callable

from .graph import AskResult

# 后台执行池。有界并发，避免长任务无限堆线程；detach 后 future 仍在池里跑完。
_POOL = _cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="askdb-agent")


def run_or_detach(fn: Callable[[], AskResult], threshold_ms: int,
                  thread_id: str) -> tuple[AskResult | None, dict[str, Any] | None]:
    """提交 fn 到后台，最多等 threshold_ms。

    返回 (result, None)  —— 在阈值内完成，同步直返；
    返回 (None, notice)  —— 超阈值，已转后台，notice 是给前端的异步提示。
    threshold_ms<=0 表示立即异步（Tier-1 预判为长任务时用）。
    """
    fut = _POOL.submit(fn)
    if threshold_ms <= 0:
        return None, _notice(thread_id)
    try:
        r = fut.result(timeout=threshold_ms / 1000.0)
        return r, None
    except _cf.TimeoutError:
        # 不取消 future —— 让它在池里跑完并写 final 审计，任务中心据此更新。
        return None, _notice(thread_id)


def _notice(thread_id: str) -> dict[str, Any]:
    return {
        "async": True,
        "thread_id": thread_id,
        "trace_id": thread_id,
        "message": "该任务耗时较长，已转后台执行；请到任务中心查看结果。",
    }
