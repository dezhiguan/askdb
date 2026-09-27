"""Agent spec defaults and tool-ceiling intersection."""

from __future__ import annotations

from askdb.agents.spec import load_agent, narrow_tools
from askdb.config import Config


def _cfg(raw: dict | None = None) -> Config:
    base = {"llm": {"model": "fake"}, "tenant": {"default_ctx": 1}}
    base.update(raw or {})
    return Config(root=".", raw=base, tables={}, metrics=[], path="t.yaml")


def test_query_defaults_match_the_current_loop():
    spec = load_agent(_cfg(), "query")
    assert spec.kind == "react"
    assert spec.skills_role == "query_worker"
    assert spec.tools == ("search_schema", "get_table_schema", "execute_sql")
    assert spec.max_steps == 6
    assert spec.model == ""
    assert not spec.max_steps_explicit
    assert not spec.model_explicit


def test_explicit_agent_fields_override_defaults():
    spec = load_agent(_cfg({"agents": {"query": {
        "max_steps": 2,
        "model": "qwen3.8-plus",
        "tools": ["execute_sql", "not_a_tool"],
    }}}), "query")
    assert spec.max_steps == 2 and spec.max_steps_explicit
    assert spec.model == "qwen3.8-plus" and spec.model_explicit
    assert spec.tools == ("execute_sql", "not_a_tool")


def test_instruction_only_skills_do_not_shrink_the_ceiling():
    registry = ["search_schema", "get_table_schema", "execute_sql", "analyze_result"]
    declared = ["search_schema", "get_table_schema", "execute_sql"]
    assert narrow_tools(declared, registry) == frozenset(declared)
    assert narrow_tools(declared, registry, []) == frozenset(declared)


def test_skill_requested_tools_can_only_shrink():
    registry = ["search_schema", "get_table_schema", "execute_sql"]
    declared = ["search_schema", "execute_sql"]
    got = narrow_tools(declared, registry, ["execute_sql", "export_result"])
    assert got == frozenset({"execute_sql"})


def test_unknown_agent_name_is_rejected():
    try:
        load_agent(_cfg(), "missing")
    except KeyError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected KeyError")
