"""Cheap deterministic routing before paying the multi-agent fixed cost."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason: str


_COMPLEX_MARKERS = (
    "分析原因", "归因", "分别看", "对比", "比较", "同比", "环比", "趋势",
    "漏斗", "留存", "转化率", "相关性", "异常", "多维", "各渠道", "各地区",
)
_JOIN_MARKERS = ("以及", "并且", "同时", "分别", "之间", "跨库", "跨数据源")


def decide_route(question: str, *, requested_mode: str = "auto",
                 source_count: int = 1) -> RouteDecision:
    mode = (requested_mode or "auto").lower()
    if mode in {"single", "fast"}:
        return RouteDecision("single", "request_forced_single")
    if mode in {"multi", "enforce"}:
        return RouteDecision("multi", "request_forced_multi")
    if source_count > 1:
        return RouteDecision("multi", "multiple_sources")
    hits = [marker for marker in _COMPLEX_MARKERS if marker in question]
    clauses = len(re.findall(r"[，,、；;]", question))
    if hits:
        return RouteDecision("multi", "complex_intent:" + hits[0])
    if clauses >= 2 and any(marker in question for marker in _JOIN_MARKERS):
        return RouteDecision("multi", "multiple_analysis_clauses")
    return RouteDecision("single", "simple_single_source")
