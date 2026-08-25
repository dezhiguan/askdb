"""Langfuse 观测上报（可选旁路）—— 不经 LangChain 集成。

本项目刻意不用 langchain 元包（README·技术栈），而 langfuse v2 的
LangChain 回调恰恰硬依赖它。因此走低层 SDK：一次调用完成后，把
**与审计记录同源的步骤链**作为 trace 上报 —— 观测面与审计面天然一致，
也天然遵守双写数据边界：只有步骤元数据、SQL 文本与 token 计量，
没有结果行、没有注入提示词。

trace id 直接复用本地 trace_id，页面可按 id 深链互跳。
上报由 SDK 后台线程异步批送；任何异常静默吞掉 —— 观测挂了
不允许反噬主链路，这是双写职责边界的既定语义。

时间轴说明：步骤只落了时长没落起止点，这里按顺序顺次铺开，
是近似还原；要看真实并发形态以本地审计的 elapsed_ms 为准。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

_client = None


def _lf():
    global _client
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY")
            and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    if _client is None:
        try:
            from langfuse import Langfuse

            _client = Langfuse()
        except Exception:
            return None
    return _client


def report(record: dict[str, Any]) -> None:
    """按审计记录的形状上报一条 trace。异常一律吞掉。"""
    lf = _lf()
    if lf is None:
        return
    try:
        end = datetime.fromisoformat(str(record["ts"]))
        start = end - timedelta(milliseconds=int(record.get("elapsed_ms") or 0))
        trace = lf.trace(
            id=record["trace_id"], name="askdb.ask", timestamp=start,
            input=record.get("question"),
            output=record.get("sql_final") or record.get("rejected_by") or "",
            metadata={
                "org_id": record.get("org_id"), "kind": record.get("kind"),
                "rejected_by": record.get("rejected_by"),
                "attempts": record.get("attempts"),
                "cost_cny": record.get("cost_cny"),
                "tables_hit": record.get("tables_hit"),
            },
            tags=[str(record.get("kind", "ask"))]
                 + (["blocked"] if record.get("rejected_by") else []),
        )
        cur = start
        for s in record.get("steps") or []:
            dur = timedelta(milliseconds=int(s.get("ms") or 0))
            kw: dict[str, Any] = {
                "name": s.get("step"), "start_time": cur, "end_time": cur + dur,
                "metadata": {"note": s.get("note"), "status": s.get("status")},
            }
            if s.get("tok_in") or s.get("tok_out"):
                trace.generation(**kw, usage={
                    "input": int(s.get("tok_in") or 0),
                    "output": int(s.get("tok_out") or 0), "unit": "TOKENS",
                })
            else:
                trace.span(**kw)
            cur += dur
    except Exception:
        pass
