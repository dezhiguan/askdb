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
from askdb.multiagent.shadow_compare import compare


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


def test_resume_follows_the_checkpoint_not_an_audit_mode(cfg, monkeypatch):
    from askdb import agentgraph
    from askdb.ask import resume_ask
    from askdb.multiagent import runtime as multi_runtime
    from askdb.multiagent import supervisor_graph

    monkeypatch.setattr(supervisor_graph, "ensure_graph", lambda _cfg: SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(values={"plan": {"plan_id": "p"}})))
    monkeypatch.setattr(multi_runtime, "resume_multi_agent", lambda *_a, **_k: "multi")
    monkeypatch.setattr(agentgraph, "resume", lambda *_a, **_k: "single")
    assert resume_ask("thread", cfg) == "multi"

    monkeypatch.setattr(supervisor_graph, "ensure_graph", lambda _cfg: SimpleNamespace(
        get_state=lambda _config: SimpleNamespace(values={"question": "有多少"})))
    assert resume_ask("thread", cfg, question="有多少") == "single"


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
    monkeypatch.setattr("askdb.agent.run_agent", fake_single)
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
    monkeypatch.setattr("askdb.agent.run_agent", lambda question, cfg, **kwargs: AskResult(
        ok=True, question=question, trace_id=kwargs["trace_id"],
        thread_id=kwargs["thread_id"], org_id=65, reasoning="single"))

    def fake_multi(question, cfg, **kwargs):
        seen["shadow_of"] = kwargs["shadow_of"]
        seen["baseline_trace"] = kwargs["shadow_baseline"].trace_id
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
    assert seen["baseline_trace"] == body["trace_id"]


def test_assist_mode_sends_complex_questions_to_multi_and_simple_ones_to_single(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from askdb import server
    from askdb.multiagent import runtime as multi_runtime

    isolated = copy.deepcopy(cfg)
    isolated.raw["multi_agent"] = {"enabled": True, "mode": "assist",
                                    "allow_cross_source": False}
    monkeypatch.setattr(server, "load", lambda _path: isolated)
    calls = {"multi": 0, "single": 0}

    def fake_multi(question, cfg, **kwargs):
        calls["multi"] += 1
        return AskResult(ok=True, question=question, trace_id=kwargs["trace_id"],
                         thread_id=kwargs["thread_id"], org_id=65,
                         reasoning="multi", execution_mode="multi")

    def fake_single(question, cfg, **kwargs):
        calls["single"] += 1
        return AskResult(ok=True, question=question, trace_id=kwargs["trace_id"],
                         thread_id=kwargs["thread_id"], org_id=65, reasoning="single")

    monkeypatch.setattr(multi_runtime, "run_multi_agent", fake_multi)
    monkeypatch.setattr("askdb.agent.run_agent", fake_single)
    client = TestClient(server.create_app("ignored.yaml"))

    complex_q = client.post("/api/ask", json={
        "question": "分析订单下降原因，分别看渠道和地区", "mode": "auto"}).json()
    assert complex_q["execution_mode"] == "multi"

    simple_q = client.post("/api/ask", json={
        "question": "documents 有多少行", "mode": "auto"}).json()
    assert simple_q.get("execution_mode", "single") == "single"
    assert calls == {"multi": 1, "single": 1}


def _peer_events():
    from askdb.sources import Source

    return Source(
        id="src_events", name="事件库", type="duckdb", dsn=":memory:",
        tables=[{"name": "events", "columns": {}}],
    )


def test_compare_without_contract_answers_the_current_source_and_names_the_other(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from askdb import server
    from askdb.multiagent import source_select

    isolated = copy.deepcopy(cfg)
    isolated.raw["multi_agent"] = {
        "enabled": True, "mode": "assist", "allow_cross_source": True,
        "join_contracts": [], "max_workers": 3,
    }
    monkeypatch.setattr(server, "load", lambda _path: isolated)
    monkeypatch.setattr(source_select, "registered_sources", lambda _cfg: [_peer_events()])

    def recall(_question, item):
        sid = getattr(item, "source_id", None) or "builtin"
        if sid == "src_events":
            return source_select.RecallHit("src_events", "事件库", ("events",))
        return source_select.RecallHit("builtin", "订单库", ("orders",))

    monkeypatch.setattr(source_select, "recall_hit", recall)
    monkeypatch.setattr(
        "askdb.agent.run_agent",
        lambda question, cfg, **kwargs: AskResult(
            ok=True, question=question, trace_id=kwargs["trace_id"],
            thread_id=kwargs["thread_id"], org_id=65, reasoning="single"))
    client = TestClient(server.create_app("ignored.yaml"))
    body = client.post("/api/ask", json={
        "question": "跨数据源看访问量", "mode": "auto"}).json()
    assert body["ok"] is True, {k: body.get(k) for k in ("rejected_by", "error", "execution_mode", "source_note")}
    assert body["execution_mode"] == "single"
    assert body["sources_used"] == [{"id": "builtin", "name": "订单库"}]
    assert body["sources_omitted"] == [{"id": "src_events", "name": "事件库"}]
    assert "事件库" in body["source_note"]


def test_expand_marker_with_a_contract_still_stays_on_the_current_source(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from askdb import server
    from askdb.multiagent import source_select

    isolated = copy.deepcopy(cfg)
    isolated.raw["multi_agent"] = {
        "enabled": True, "mode": "assist", "allow_cross_source": True,
        "max_workers": 3,
        "join_contracts": [{
            "contract_id": "orders_events_day",
            "left_source": "builtin",
            "right_source": "src_events",
            "join_keys": ["date"],
            "grain": "day",
            "aggregate_only": True,
        }],
    }
    monkeypatch.setattr(server, "load", lambda _path: isolated)
    monkeypatch.setattr(source_select, "registered_sources", lambda _cfg: [_peer_events()])

    def recall(_question, item):
        sid = getattr(item, "source_id", None) or "builtin"
        if sid == "src_events":
            return source_select.RecallHit("src_events", "事件库", ("events",))
        return source_select.RecallHit("builtin", "订单库", ("orders",))

    monkeypatch.setattr(source_select, "recall_hit", recall)
    monkeypatch.setattr(
        "askdb.agent.run_agent",
        lambda question, cfg, **kwargs: AskResult(
            ok=True, question=question, trace_id=kwargs["trace_id"],
            thread_id=kwargs["thread_id"], org_id=65, reasoning="single"))
    monkeypatch.setattr(
        "askdb.multiagent.runtime.run_multi_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不该汇总")))
    client = TestClient(server.create_app("ignored.yaml"))
    body = client.post("/api/ask", json={
        "question": "跨数据源看访问量", "mode": "auto"}).json()
    assert body["ok"] is True, {k: body.get(k) for k in ("rejected_by", "error", "execution_mode", "source_note")}
    assert body["execution_mode"] == "single"
    assert body["sources_used"] == [{"id": "builtin", "name": "订单库"}]
    assert body["sources_omitted"] == [{"id": "src_events", "name": "事件库"}]
    assert "事件库" in body["source_note"]


def test_explicit_single_mode_does_not_look_at_other_sources(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from askdb import server
    from askdb.multiagent import source_select

    isolated = copy.deepcopy(cfg)
    isolated.raw["multi_agent"] = {
        "enabled": True, "mode": "assist", "allow_cross_source": True,
        "join_contracts": [], "max_workers": 3,
    }
    monkeypatch.setattr(server, "load", lambda _path: isolated)
    monkeypatch.setattr(source_select, "registered_sources", lambda _cfg: [_peer_events()])
    monkeypatch.setattr(
        source_select, "plan_sources",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mode=single 不该选源")))
    monkeypatch.setattr(
        "askdb.agent.run_agent",
        lambda question, cfg, **kwargs: AskResult(
            ok=True, question=question, trace_id=kwargs["trace_id"],
            thread_id=kwargs["thread_id"], org_id=65, reasoning="single"))
    client = TestClient(server.create_app("ignored.yaml"))
    body = client.post("/api/ask", json={
        "question": "对比订单和访问", "mode": "single"}).json()
    assert body["ok"] is True
    assert body.get("source_note", "") == ""


def test_blind_current_source_rejects_instead_of_merging(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from askdb import server
    from askdb.multiagent import source_select

    isolated = copy.deepcopy(cfg)
    isolated.raw["multi_agent"] = {
        "enabled": True, "mode": "assist", "allow_cross_source": False,
        "join_contracts": [], "max_workers": 3,
    }
    monkeypatch.setattr(server, "load", lambda _path: isolated)
    monkeypatch.setattr(source_select, "registered_sources", lambda _cfg: [_peer_events()])

    def recall(_question, item):
        sid = getattr(item, "source_id", None) or "builtin"
        if sid == "src_events":
            return source_select.RecallHit("src_events", "事件库", ("events",))
        return source_select.RecallHit("builtin", "订单库", (), blind=True)

    monkeypatch.setattr(source_select, "recall_hit", recall)
    called = {"agent": 0}

    def fake_single(*args, **kwargs):
        called["agent"] += 1
        return AskResult(ok=True, question="x", trace_id="t", thread_id="t", org_id=65)

    monkeypatch.setattr("askdb.agent.run_agent", fake_single)
    client = TestClient(server.create_app("ignored.yaml"))
    body = client.post("/api/ask", json={"question": "访问有多少", "mode": "auto"}).json()
    assert body["ok"] is False
    assert body["rejected_by"] == "P12"
    assert "订单库" in body["error"] and "事件库" in body["error"]
    assert called["agent"] == 0


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


def test_shadow_compares_sql_data_conclusion_and_evidence():
    single = AskResult(ok=True, question="数量", trace_id="single", org_id=1,
                       sql_final="SELECT count(*) FROM documents", rows=[[203]],
                       reasoning="共有203个，约两个数量级", evidence=[{
                           "evidence_id": "e1", "sql_final": "SELECT count(*) FROM documents",
                           "rows": [[203]], "checksum": "sha256:a"}],
                       reviews=[{"verdict": "PASS"}],
                       claims=[{"evidence_ids": ["e1"]}])
    shadow = AskResult(ok=True, question="数量", trace_id="shadow", org_id=1,
                       sql_final="select count(*) from documents", rows=[[203]],
                       reasoning="共有 203 个，约三个数量级", evidence=[{
                           "evidence_id": "e2", "sql_final": "select count(*) from documents",
                           "rows": [["203"]], "checksum": "sha256:b"}],
                       reviews=[{"verdict": "PASS"}],
                       claims=[{"evidence_ids": ["e2"]}])
    report = compare(single, shadow)
    assert report["layers"]["sql"]["same"] is True
    assert report["layers"]["data"]["same"] is True
    assert report["layers"]["conclusion"]["same"] is False
    assert report["layers"]["conclusion"]["magnitude_claims_same"] is False
    assert report["layers"]["evidence"]["same"] is True
    assert report["status"] == "DIFFERENT"
    assert "203" not in str(report), "comparison metadata must not copy result rows"


def test_cancel_accepts_request_before_first_checkpoint(cfg, monkeypatch):
    fake_graph = SimpleNamespace(get_state=lambda _config: SimpleNamespace(values={}))
    monkeypatch.setattr(runtime, "ensure_graph", lambda _cfg: fake_graph)
    runtime._CANCEL_EVENTS.pop("early", None)
    assert runtime.cancel("early", cfg) is True
    assert runtime._CANCEL_EVENTS["early"].is_set()
    runtime._CANCEL_EVENTS.pop("early", None)


def test_late_worker_cannot_publish_success_after_durable_cancel(cfg, monkeypatch):
    from askdb.audit import AuditFilter, iter_records
    from askdb.trace import write_audit

    thread_id = "a1b2c3d4e5f6"

    class FakeGraph:
        def invoke(self, state, _config):
            write_audit(cfg, {
                "trace_id": thread_id, "thread_id": thread_id,
                "kind": "cancel", "rejected_by": "CANCELED", "phase": "final",
                "question": state["question"], "execution_mode": "multi",
            })
            return {**state, "status": "COMPLETED", "answer": "late success"}

    monkeypatch.setattr(runtime, "ensure_graph", lambda _cfg: FakeGraph())
    runtime._CANCEL_EVENTS.pop(thread_id, None)
    result = runtime.run_multi_agent("分析渠道和地区", cfg,
                                     thread_id=thread_id, trace_id=thread_id)
    assert result.rejected_by == "CANCELED" and not result.ok
    records = list(iter_records(cfg, AuditFilter(include_started=True,
                                                thread_ids=(thread_id,))))
    assert len(records) == 2  # started + authoritative cancellation
    assert records[-1]["rejected_by"] == "CANCELED"
    runtime._CANCEL_EVENTS.pop(thread_id, None)


def test_cancel_remains_terminal_even_if_a_late_success_record_exists(cfg):
    from askdb import audit, auditstore
    from askdb.trace import write_audit

    thread_id = "b1b2c3d4e5f6"
    common = {"thread_id": thread_id, "trace_id": thread_id,
              "question": "测试取消终态", "kind": "ask"}
    write_audit(cfg, {**common, "phase": "started"})
    write_audit(cfg, {**common, "phase": "final", "rejected_by": "CANCELED"})
    write_audit(cfg, {**common, "phase": "final", "rejected_by": None,
                      "answer": "late success"})
    assert audit.tasks(cfg.audit_log)[0]["status"] == audit.CANCELED
    sql, _ = auditstore._thread_cte(
        audit.AuditFilter(include_started=True), audit.TaskFold(), None)
    assert "max(id) FILTER (WHERE rejected_by = 'CANCELED')" in sql
