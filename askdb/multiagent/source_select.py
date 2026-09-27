"""Pick which authorized sources a question may read.

The current source is always first. Other sources are recalled only when the
current recall is blind, has coverage gaps, or the question asks to compare
across sources. A second source is queried only when it hits tables the
current source did not, and every pair has an aggregate join contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from .federation import FederationPolicyError, approved_contracts, parse_contracts
from .protocol import JoinContract

EXPAND_MARKERS = ("分别", "对比", "跨数据源")


@dataclass(frozen=True)
class RecallHit:
    source_id: str
    name: str
    tables: tuple[str, ...] = ()
    blind: bool = False
    coverage_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourcePlan:
    action: str
    selected: tuple[RecallHit, ...]
    omitted: tuple[RecallHit, ...] = ()
    reason: str = ""

    def as_used(self) -> list[dict[str, str]]:
        return [{"id": hit.source_id, "name": hit.name} for hit in self.selected]

    def as_omitted(self) -> list[dict[str, str]]:
        return [{"id": hit.source_id, "name": hit.name} for hit in self.omitted]


def hold_unjoined(plan: SourcePlan) -> SourcePlan:
    """Orchestration cannot run. Keep a current-source answer, or refuse."""
    current = plan.selected[0]
    extras = tuple(plan.selected[1:])
    if current.tables and not current.blind:
        names = "、".join(hit.name or hit.source_id for hit in extras)
        return SourcePlan(
            "degrade", (current,), extras,
            f"另有相关表在{names}，未参与汇总")
    left = current.name or current.source_id
    right = extras[0].name or extras[0].source_id if extras else ""
    return SourcePlan(
        "reject", (current,), extras,
        f"数据源 {left} 与 {right} 没有已登记的聚合 JoinContract")


def expansion_needed(question: str, current: RecallHit) -> bool:
    if current.blind or current.coverage_gaps:
        return True
    return any(marker in question for marker in EXPAND_MARKERS)


def _fresh(current: RecallHit, other: RecallHit) -> tuple[str, ...]:
    owned = set(current.tables)
    return tuple(table for table in other.tables if table not in owned)


def plan_sources(
    question: str,
    current: Config,
    others: list[Config],
    *,
    allow_cross_source: bool,
    contracts: list[dict],
    max_sources: int,
    recall,
) -> SourcePlan:
    """Recall the current source, then peers only when expansion is warranted."""
    current_hit = recall(question, current)
    alone = SourcePlan("single", (current_hit,))
    if not others or max_sources <= 1 or not expansion_needed(question, current_hit):
        return alone
    extras: list[tuple[int, RecallHit]] = []
    for other in others:
        try:
            hit = recall(question, other)
        except Exception:
            continue
        if hit.blind:
            continue
        fresh = _fresh(current_hit, hit)
        if fresh:
            extras.append((len(fresh), hit))
    extras.sort(key=lambda item: (-item[0], item[1].source_id))
    picked = [item[1] for item in extras[: max_sources - 1]]
    if not picked:
        return alone
    parsed = _contracts(contracts)
    if allow_cross_source and _covered(
            {current_hit.source_id, *(hit.source_id for hit in picked)}, parsed):
        return SourcePlan("multi", (current_hit, *picked))
    if current_hit.tables and not current_hit.blind:
        names = "、".join(hit.name or hit.source_id for hit in picked)
        return SourcePlan(
            "degrade", (current_hit,), tuple(picked),
            f"另有相关表在{names}，未参与汇总")
    left = current_hit.name or current_hit.source_id
    right = picked[0].name or picked[0].source_id
    return SourcePlan(
        "reject", (current_hit,), tuple(picked),
        f"数据源 {left} 与 {right} 没有已登记的聚合 JoinContract")


def recall_hit(question: str, cfg: Config) -> RecallHit:
    from ..tools import search_schema

    found = search_schema(question, cfg)
    data = found.data or {}
    return RecallHit(
        source_id=cfg.source_id or "builtin",
        name=cfg.source_name or cfg.source_id or "当前数据源",
        tables=tuple(data.get("tables") or ()),
        blind=bool(data.get("blind")),
        coverage_gaps=tuple(data.get("coverage_gaps") or ()),
    )


def registered_sources(cfg: Config) -> list:
    """Runtime sources, or an empty list when the registry cannot be read."""
    from ..sources import list_sources

    try:
        return list_sources(cfg)
    except Exception:
        return []


def _contracts(raw: list[dict]) -> list[JoinContract]:
    try:
        return parse_contracts(raw or [])
    except Exception:
        return []


def _covered(source_ids: set[str], contracts: list[JoinContract]) -> bool:
    try:
        approved_contracts(source_ids, contracts)
    except FederationPolicyError:
        return False
    return True
