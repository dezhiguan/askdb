from __future__ import annotations

import copy

import pytest

from askdb.graph import AskResult
from askdb.multiagent import runtime
from askdb.multiagent.skills import build_registry
from askdb.multiagent.skills.store import create_draft, mark_tested, publish, set_status
from askdb.multiagent.skills.manifest import SkillStatus
from askdb.trace import Tracer


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
