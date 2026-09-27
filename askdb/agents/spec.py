"""Reusable agent units: model, tools, skills and step budget.

An agent does not know whether it was started on its own or placed in the
orchestration graph. Missing fields inherit the process-wide ``llm`` block and
the role defaults below. ``agents.<name>`` wins over ``agent.max_steps`` when
both set a step budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


_KINDS = frozenset({"react", "structured"})


@dataclass(frozen=True)
class AgentSpec:
    name: str
    kind: str
    tools: tuple[str, ...]
    skills_role: str
    max_steps: int | None = None
    model: str = ""
    cost_cap_tokens: int | None = None
    max_steps_explicit: bool = False
    model_explicit: bool = False
    cost_cap_explicit: bool = False


#: Built-in roles. Tool lists are the ceiling; instruction-only Skills do not
#: shrink them. A Skill that names tools intersects with this list.
DEFAULTS: dict[str, AgentSpec] = {
    "query": AgentSpec(
        "query", "react",
        ("search_schema", "get_table_schema", "execute_sql"),
        "query_worker", max_steps=6),
    "supervisor": AgentSpec(
        "supervisor", "structured", (), "supervisor"),
    "semantic": AgentSpec(
        "semantic", "structured",
        ("search_schema", "get_table_schema"),
        "semantic"),
    "verifier": AgentSpec(
        "verifier", "react",
        ("get_table_schema", "execute_sql"),
        "verifier", max_steps=3),
    "synthesizer": AgentSpec(
        "synthesizer", "structured", (), "synthesizer"),
}


def load_agent(cfg: Any, name: str) -> AgentSpec:
    """Merge ``agents.<name>`` over the built-in spec. Unknown names raise."""
    base = DEFAULTS.get(name)
    if base is None:
        known = ", ".join(sorted(DEFAULTS))
        raise KeyError(f"未知智能体 {name}，已知：{known}")
    raw = (getattr(cfg, "raw", {}) or {}).get("agents") or {}
    item = raw.get(name) or {}
    if not isinstance(item, dict):
        raise ValueError(f"agents.{name} 必须是映射")
    kind = str(item.get("kind", base.kind) or base.kind).lower()
    if kind not in _KINDS:
        raise ValueError(f"agents.{name}.kind 只能是 react 或 structured")
    tools = base.tools
    if "tools" in item:
        tools = tuple(str(t) for t in (item.get("tools") or []))
    max_steps = base.max_steps
    max_steps_explicit = "max_steps" in item
    if max_steps_explicit:
        max_steps = int(item.get("max_steps") or 0)
        if max_steps < 1:
            raise ValueError(f"agents.{name}.max_steps 必须 ≥ 1")
    model = base.model
    model_explicit = "model" in item and bool(str(item.get("model") or "").strip())
    if model_explicit:
        model = str(item.get("model")).strip()
    cost_cap = base.cost_cap_tokens
    cost_cap_explicit = "cost_cap_tokens" in item
    if cost_cap_explicit:
        cost_cap = int(item.get("cost_cap_tokens") or 0)
        if cost_cap < 1:
            raise ValueError(f"agents.{name}.cost_cap_tokens 必须 ≥ 1")
    role = str(item.get("skills_role") or base.skills_role)
    return AgentSpec(
        name=name, kind=kind, tools=tools, skills_role=role,
        max_steps=max_steps, model=model, cost_cap_tokens=cost_cap,
        max_steps_explicit=max_steps_explicit,
        model_explicit=model_explicit,
        cost_cap_explicit=cost_cap_explicit,
    )


def narrow_tools(declared: Iterable[str], registry: Iterable[str],
                 requested: Iterable[str] = ()) -> frozenset[str]:
    """Ceiling is the spec; registry drops unknown names.

    Instruction-only Skills request nothing and leave the ceiling intact.
    A Skill that names tools intersects with the ceiling and can only shrink it.
    """
    base = frozenset(declared) & frozenset(registry)
    asked = frozenset(str(item) for item in requested if item)
    if not asked:
        return base
    return base & asked
