"""Pick which authorized sources a question may read.

The current source is always first. Other sources are recalled only when the
current recall is blind, has coverage gaps, or the question asks to compare
across sources. A second source is queried only when it hits tables the
current source did not, and every pair has an aggregate join contract.
Among those, a source that names an entity the current source's tables do not
comes first. Catalog size is only the fallback when the question names none.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..schema_rag import CN_HINTS, WEAK_HINTS, _tokens
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
    fk_added: tuple[str, ...] = ()


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
    return tuple(table for table in _relevance_tables(other) if table not in owned)


def _relevance_tables(hit: RecallHit) -> tuple[str, ...]:
    """Tables the recall ranked for this question, without FK neighbors."""
    fk = set(hit.fk_added)
    ranked = tuple(table for table in hit.tables if table not in fk)
    return ranked or tuple(hit.tables)


def _question_terms(question: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (cn, ens) for cn, ens in CN_HINTS.items()
        if cn in question and cn not in WEAK_HINTS)


def _covered_terms(tables: tuple[str, ...],
                   terms: tuple[tuple[str, tuple[str, ...]], ...]) -> set[str]:
    covered: set[str] = set()
    for name in tables:
        tokens = _tokens(name)
        for cn, ens in terms:
            if tokens & set(ens):
                covered.add(cn)
    return covered


def _exclusive_terms(question: str, current: RecallHit, other: RecallHit) -> set[str]:
    """Entities this peer's own tables name and the current source's tables do not."""
    terms = _question_terms(question)
    if not terms:
        return set()
    owned = _covered_terms(_relevance_tables(current), terms)
    return _covered_terms(_fresh(current, other), terms) - owned


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
    extras: list[tuple[int, int, RecallHit]] = []
    for other in others:
        try:
            hit = recall(question, other)
        except Exception:
            continue
        if hit.blind:
            continue
        fresh = _fresh(current_hit, hit)
        if not fresh:
            continue
        exclusive = _exclusive_terms(question, current_hit, hit)
        extras.append((len(exclusive), len(fresh), hit))
    # A peer that names an entity the current tables do not outranks a larger
    # catalog. When the question names no such entity, table count remains
    # the tie-break so a blind current source still prefers a real hit.
    named = [item for item in extras if item[0] > 0]
    pool = named or extras
    pool.sort(key=lambda item: (-item[0], -item[1], item[2].source_id))
    picked = [item[2] for item in pool[: max_sources - 1]]
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
        fk_added=tuple(data.get("fk_added") or ()),
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
