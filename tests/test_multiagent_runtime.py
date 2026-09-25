from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from askdb.graph import AskResult
from askdb import skill
from askdb.multiagent import runtime
from askdb.multiagent.skills import build_registry
from askdb.multiagent.skills.store import create_draft, mark_tested, publish, rollback, set_status
from askdb.multiagent.federation import FederationPolicyError, approved_contracts, parse_contracts
from askdb.multiagent.skills.manifest import SkillStatus
from askdb.trace import Tracer
from askdb.qcache import scope as scope_fingerprint


def _skill_payload(skill_id: str = "orders.mom") -> dict:
    return {
        "id": skill_id,
        "version": "1.0.0",
        "owner": "commerce",
        "kind": "analysis",
        "agent_roles": ["query_worker"],
        "source_scopes": ["*"],
        "triggers": {"terms": ["环比"]},
        "requested_tools": ["execute_sql"],
        "instructions": ["比较期与当前期必须等长"],
        "tests": [{"name": "触发环比", "question": "订单环比", "should_match": True}],
    }


def test_skill_lifecycle_persists_draft_test_and_publish(cfg, tmp_path):
    cfg.raw["skills"] = {"path": str(tmp_path / "skills.json"), "mode": "shadow"}
    draft = create_draft(cfg, _skill_payload())
    assert draft.status == "draft"
    assert mark_tested(cfg, draft.id, draft.version)["ok"] is True
    released = publish(cfg, draft.id, draft.version)
    assert released.status == "published"
    stored = build_registry(cfg).get(draft.id, draft.version)
    assert stored is not None and stored.checksum == released.checksum
    with pytest.raises(ValueError, match="已存在"):
        create_draft(cfg, _skill_payload())


def test_skill_shadow_gate_requires_tests_and_unknown_tools_are_rejected(cfg, tmp_path):
    cfg.raw["skills"] = {"path": str(tmp_path / "skills.json")}
    draft = create_draft(cfg, _skill_payload("orders.unsafe_tool") | {
        "requested_tools": ["shell"]})
    with pytest.raises(ValueError, match="必须先通过"):
        set_status(cfg, draft.id, draft.version, SkillStatus.SHADOW)
    assert mark_tested(cfg, draft.id, draft.version)["ok"]
    with pytest.raises(ValueError, match="未注册 Tool"):
        publish(cfg, draft.id, draft.version)


def test_skill_can_rollback_to_a_tested_older_version(cfg, tmp_path):
    cfg.raw["skills"] = {"path": str(tmp_path / "skills.json")}
    first = create_draft(cfg, _skill_payload("orders.versioned"))
    mark_tested(cfg, first.id, first.version)
    publish(cfg, first.id, first.version)
    second = create_draft(cfg, _skill_payload("orders.versioned") | {"version": "2.0.0"})
    mark_tested(cfg, second.id, second.version)
    publish(cfg, second.id, second.version)

    restored = rollback(cfg, first.id, first.version)
    registry = build_registry(cfg)
    assert restored.status == "published"
    assert registry.get(first.id, "2.0.0").status == "disabled"


def test_checkpoint_rehydrates_the_exact_pinned_skill_version(cfg, tmp_path):
    cfg.raw["skills"] = {"path": str(tmp_path / "skills.json"), "mode": "enforce"}
    first = create_draft(cfg, _skill_payload("orders.pinned"))
    mark_tested(cfg, first.id, first.version)
    publish(cfg, first.id, first.version)
    resolved = skill.resolve(
        cfg, role="query_worker", source_id="builtin", question="订单环比",
        runtime_allowed_tools=("execute_sql",), agent_allowed_tools=("execute_sql",))
    pinned = [item.model_dump(mode="json") for item in resolved.bindings]

    second = create_draft(cfg, _skill_payload("orders.pinned") | {"version": "2.0.0"})
    mark_tested(cfg, second.id, second.version)
    publish(cfg, second.id, second.version)

    restored = skill.load_pinned(cfg, pinned)
    selected = next(item for item in restored.bindings if item.skill_id == "orders.pinned")
    assert selected.version == "1.0.0"


def test_cross_source_requires_an_explicit_aggregate_join_contract():
    with pytest.raises(FederationPolicyError, match="JoinContract"):
        approved_contracts({"orders", "events"}, [])
    contracts = parse_contracts([{
        "contract_id": "orders_events_day", "left_source": "orders",
        "right_source": "events", "join_keys": ["date", "campaign_id"],
        "grain": "day_campaign", "aggregate_only": True,
    }])
    assert approved_contracts({"orders", "events"}, contracts)[0].contract_id == "orders_events_day"


def test_runtime_adapter_keeps_multiagent_artifacts_and_legacy_result_shape(cfg):
    state = {
        "question": "比较渠道", "run_id": "run", "thread_id": "thread", "org_id": 65,
        "status": "COMPLETED", "answer": "渠道 A 较高", "semantic_contract": {
            "metric_definition": "订单数", "time_window": "本月", "grain": "渠道"},
        "plan": {"plan_id": "p", "subtasks": []},
        "subtasks_by_id": {"st": {"subtask_id": "st", "title": "渠道",
                                        "status": "SUCCEEDED", "attempt": 1}},
        "evidence_by_id": {"ev": {
            "evidence_id": "ev", "subtask_id": "st", "sql_final": "SELECT 1",
            "columns": ["n"], "rows": [[1]], "row_count": 1,
            "checksum": "sha256:x", "masked_columns": [],
        }},
        "reviews_by_id": {"rv": {"review_id": "rv", "verdict": "PASS"}},
        "claims": [{"claim_id": "cl", "text": "渠道 A 较高", "evidence_ids": ["ev"]}],
        "skill_bindings_by_role": {"semantic": [{
            "skill_id": "core.metric", "version": "1.0.0", "checksum": "sha256:s"}]},
    }
    result = runtime.to_result(state, cfg, Tracer())
    payload = result.to_dict()
    assert payload["execution_mode"] == "multi"
    assert payload["sql_final"] == "SELECT 1" and payload["rows"] == [[1]]
    assert payload["evidence"][0]["evidence_id"] == "ev"
    assert payload["plan"]["subtasks"][0]["status"] == "SUCCEEDED"
    assert payload["skill_bindings"][0]["skill_id"] == "core.metric"


def test_server_explicit_multi_mode_uses_multi_runtime(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from askdb import server
    from askdb.multiagent import runtime as multi_runtime

    isolated = copy.deepcopy(cfg)
    isolated.raw["skills"] = {"path": "/tmp/askdb-test-no-skills.json"}
    isolated.raw["multi_agent"] = {"enabled": True, "mode": "shadow",
                                    "allow_cross_source": False}
    monkeypatch.setattr(server, "load", lambda _path: isolated)
    calls = {"multi": 0, "single": 0}

    def fake_multi(question, cfg, **kwargs):
        calls["multi"] += 1
        return AskResult(ok=True, question=question, trace_id="m" * 12,
                         thread_id="m" * 12, org_id=65,
                         reasoning="multi", execution_mode="multi")

    def fake_single(question, cfg, **kwargs):
        calls["single"] += 1
        return AskResult(ok=True, question=question, trace_id="s" * 12,
                         thread_id="s" * 12, org_id=65, reasoning="single")

    monkeypatch.setattr(multi_runtime, "run_multi_agent", fake_multi)
    monkeypatch.setattr(server, "run_agent", fake_single)
    client = TestClient(server.create_app("ignored.yaml"))
    response = client.post("/api/ask", json={"question": "分析渠道", "mode": "multi"})
    assert response.status_code == 200
    assert response.json()["execution_mode"] == "multi"
    assert calls == {"multi": 1, "single": 0}


def test_shadow_mode_runs_multiagent_in_background_but_returns_single(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from askdb import server
    from askdb.multiagent import runtime as multi_runtime

    isolated = copy.deepcopy(cfg)
    isolated.raw["multi_agent"] = {"enabled": True, "mode": "shadow",
                                    "allow_cross_source": False}
    monkeypatch.setattr(server, "load", lambda _path: isolated)
    seen: dict[str, str] = {}
    monkeypatch.setattr(server, "run_agent", lambda question, cfg, **kwargs: AskResult(
        ok=True, question=question, trace_id=kwargs["trace_id"],
        thread_id=kwargs["thread_id"], org_id=65, reasoning="single"))

    def fake_multi(question, cfg, **kwargs):
        seen["shadow_of"] = kwargs["shadow_of"]
        return AskResult(ok=True, question=question, trace_id=kwargs["trace_id"],
                         thread_id=kwargs["thread_id"], org_id=65,
                         execution_mode="multi")

    monkeypatch.setattr(multi_runtime, "run_multi_agent", fake_multi)
    monkeypatch.setattr(server._async_runner, "submit_background",
                        lambda fn, **kwargs: (fn(), True)[1])
    client = TestClient(server.create_app("ignored.yaml"))
    body = client.post("/api/ask", json={
        "question": "分析订单下降原因，分别看渠道和地区", "mode": "auto"}).json()
    assert body["execution_mode"] == "single"
    assert seen["shadow_of"] == body["trace_id"]


def test_resume_blocks_when_current_scope_fingerprint_changed(cfg, monkeypatch):
    expected = scope_fingerprint(cfg)
    fake_graph = SimpleNamespace(get_state=lambda _config: SimpleNamespace(values={
        "plan": {"plan_id": "p"}, "source_id": "builtin", "question": "q",
        "run_id": "run", "org_id": 65,
        "scope_fingerprints": {"builtin": expected},
    }))
    monkeypatch.setattr(runtime, "ensure_graph", lambda _cfg: fake_graph)
    cfg.raw["guard"] = {**cfg.raw["guard"], "max_rows": cfg.max_rows + 1}

    result = runtime.resume_multi_agent("thread", cfg)
    assert result is not None and result.rejected_by == "RESUME_BLOCKED"
    assert "配置已变化" in result.error


def test_cross_source_runtime_fails_closed_without_join_contract(cfg):
    cfg.raw["multi_agent"] = {"enabled": True, "mode": "enforce",
                               "allow_cross_source": True, "join_contracts": []}
    other = copy.deepcopy(cfg)
    other.source_id = "events"
    result = runtime.run_multi_agent(
        "计算访问到支付转化率", cfg,
        source_configs={"builtin": cfg, "events": other})
    assert result.ok is False and result.rejected_by == "P12"


def test_canceled_checkpoint_is_never_resumable(cfg, monkeypatch):
    fake_graph = SimpleNamespace(get_state=lambda _config: SimpleNamespace(
        values={"plan": {"plan_id": "p"}, "status": "CANCELED"},
        next=("query_worker",),
    ))
    monkeypatch.setattr(runtime, "ensure_graph", lambda _cfg: fake_graph)
    assert runtime.is_resumable("thread", cfg) is False


def test_cancel_persists_terminal_state_before_signaling_workers(cfg, monkeypatch):
    updates: list[dict] = []
    fake_graph = SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(values={
            "plan": {"plan_id": "p"}, "status": "RUNNING"}),
        update_state=lambda _config, value: updates.append(value),
    )
    monkeypatch.setattr(runtime, "ensure_graph", lambda _cfg: fake_graph)
    runtime._CANCEL_EVENTS.pop("thread", None)

    assert runtime.cancel("thread", cfg) is True
    assert updates == [{"phase": "CANCELED", "status": "CANCELED"}]
    assert runtime._CANCEL_EVENTS["thread"].is_set()
    runtime._CANCEL_EVENTS.pop("thread", None)
