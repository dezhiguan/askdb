from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from askdb import skill
from askdb.multiagent.protocol import AgentRole
from askdb.multiagent.skills import (
    ResolutionContext,
    ResolutionError,
    SkillManifest,
    SkillRegistry,
    build_registry,
    resolve_skills,
)


def _manifest(skill_id: str, **overrides) -> SkillManifest:
    payload = {
        "id": skill_id,
        "version": "1.0.0",
        "status": "published",
        "owner": "test",
        "agent_roles": ["query_worker"],
        "source_scopes": ["orders_*"],
        "instructions": [f"apply {skill_id}"],
    }
    payload.update(overrides)
    return SkillManifest.model_validate(payload)


def _context(**overrides) -> ResolutionContext:
    values = {
        "agent_role": AgentRole.QUERY_WORKER,
        "source_id": "orders_prod",
        "question": "比较订单环比",
        "runtime_allowed_tools": frozenset({"execute_sql", "read_schema"}),
        "agent_allowed_tools": frozenset({"execute_sql"}),
    }
    values.update(overrides)
    return ResolutionContext(**values)


def test_manifest_rejects_secrets_and_executable_code():
    with pytest.raises(ValidationError, match="secret or executable"):
        _manifest("unsafe.secret", instructions=["api_key = abc"])
    with pytest.raises(ValidationError, match="secret or executable"):
        _manifest("unsafe.code", instructions=["run os.system('rm')"])


def test_resolver_discovers_trigger_expands_dependency_and_pins_versions():
    base = _manifest("core.time_window", source_scopes=["*"])
    old = _manifest("orders.month_over_month", version="1.1.0",
                    triggers={"terms": ["环比"]}, requires=["core.time_window@^1"],
                    requested_tools=["execute_sql", "dangerous_tool"])
    latest = old.model_copy(update={"version": "1.2.0", "checksum": ""})
    latest = SkillManifest.model_validate(latest.model_dump())
    registry = SkillRegistry([base, old, latest])

    report = resolve_skills(registry, _context())

    refs = {(item.skill_id, item.version) for item in report.bindings}
    assert ("orders.month_over_month", "1.2.0") in refs
    assert ("core.time_window", "1.0.0") in refs
    binding = next(item for item in report.bindings
                   if item.skill_id == "orders.month_over_month")
    assert binding.selection_reason == "term:环比"
    assert binding.effective_tools == ["execute_sql"]
    assert binding.checksum.startswith("sha256:")


def test_resolver_fails_closed_for_missing_dependency_conflict_and_cycle():
    missing = _manifest("orders.analysis", requires=["core.missing@^1"])
    with pytest.raises(ResolutionError, match="missing published dependency"):
        resolve_skills(SkillRegistry([missing]), _context())

    left = _manifest("orders.left", conflicts_with=["orders.right"])
    right = _manifest("orders.right")
    with pytest.raises(ResolutionError, match="skill conflict"):
        resolve_skills(SkillRegistry([left, right]), _context())

    first = _manifest("orders.first", requires=["orders.second@1"])
    second = _manifest("orders.second", requires=["orders.first@1"])
    with pytest.raises(ResolutionError, match="dependency cycle"):
        resolve_skills(SkillRegistry([first, second]), _context())


def test_role_source_status_and_trigger_are_filtered():
    registry = SkillRegistry([
        _manifest("orders.match", triggers={"terms": ["环比"]}),
        _manifest("orders.wrong_source", source_scopes=["crm_*"]),
        _manifest("orders.wrong_role", agent_roles=["verifier"]),
        _manifest("orders.draft", status="draft"),
        _manifest("orders.no_trigger", triggers={"terms": ["留存"]}),
    ])
    report = resolve_skills(registry, _context())
    assert [item.skill_id for item in report.bindings] == ["orders.match"]
    reasons = {item["reason"] for item in report.rejected}
    assert {"source_out_of_scope", "role_not_allowed", "status_not_active",
            "trigger_not_matched"} <= reasons


def test_legacy_rules_are_migrated_to_a_versioned_builtin():
    cfg = SimpleNamespace(
        raw={"skill": {"rules": ["营收以实付为准"]}}, source_id="orders_prod")
    registry = build_registry(cfg)
    legacy = registry.get("builtin.legacy_rules", "1.0.0")
    assert legacy is not None
    assert legacy.instructions == ["营收以实付为准"]
    assert legacy.checksum.startswith("sha256:")
    assert "营收以实付为准" in skill.render(cfg, source_id="orders_prod")


def test_shadow_skill_only_loads_in_shadow_mode():
    shadow = _manifest("orders.shadow", status="shadow")
    registry = SkillRegistry([shadow])
    assert resolve_skills(registry, _context(include_shadow=False)).bindings == []
    assert resolve_skills(registry, _context(include_shadow=True)).bindings
