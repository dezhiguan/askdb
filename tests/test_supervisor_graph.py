from __future__ import annotations

import threading
import multiprocessing
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from askdb import tools
from askdb.multiagent.router import decide_route
from askdb.multiagent.state import initial_state, merge_by_id
from askdb.multiagent.supervisor_graph import (
    MultiAgentDeps,
    PlannedAnalysis,
    SemanticContractDraft,
    SupervisorPlanDraft,
    SynthesisDraft,
    build_graph,
    ensure_graph,
    reset_graph,
)
from askdb.trace import Tracer


def _run_until_killed(cfg, state, entered):
    """A real OS process owns the graph until the parent kills it mid-Worker."""
    from askdb import tools as child_tools

    def blocked_sql(*_args, **_kwargs):
        entered.set()
        threading.Event().wait(60)

    child_tools.execute_sql = blocked_sql
    reset_graph()
    ensure_graph(cfg).invoke(state, {"configurable": {
        "thread_id": state["thread_id"], "deps": _deps(cfg)}})


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cost_cny = 0.01


class _CoordinatorLlm:
    def structured(self, schema, system, human):
        if schema is SupervisorPlanDraft:
            return SupervisorPlanDraft(
                reasoning="按渠道与地区并行取证",
                analyses=[
                    PlannedAnalysis(title="渠道", question="按渠道分析订单"),
                    PlannedAnalysis(title="地区", question="按地区分析订单"),
                ],
            ), _Usage()
        if schema is SemanticContractDraft:
            return SemanticContractDraft(
                metric_definition="订单数=去重订单ID",
                time_window="本月",
                comparison_window="上月",
                grain="维度",
            ), _Usage()
        if schema is SynthesisDraft:
            return SynthesisDraft(
                answer="渠道与地区证据均已核验。",
                claims=["渠道结果已核验", "地区结果已核验"],
            ), _Usage()
        raise AssertionError(schema)


class _WorkerLlm:
    def generate_sql(self, question, schema_prompt, **kwargs):
        marker = "channel" if "渠道" in question else "region"
        return SimpleNamespace(sql=f"SELECT '{marker}' AS dimension", reasoning=""), _Usage()


class _Executor:
    def close(self):
        pass


def _deps(cfg):
    return MultiAgentDeps(
        cfg=cfg,
        llm=_CoordinatorLlm(),
        tracer=Tracer(),
        llm_factory=lambda _cfg: _WorkerLlm(),
        executor_factory=lambda _cfg: _Executor(),
    )


def _patch_tools(monkeypatch, *, fail_first_region=False):
    monkeypatch.setattr(tools, "search_schema", lambda question, cfg: tools.ToolResult(
        ok=True, tool="search_schema",
        data={"prompt": "orders(id, channel, region)", "tables": ["orders"]},
    ))
    calls = {"region": 0}

    def execute(sql, cfg, org_id, executor):
        dimension = "region" if "region" in sql else "channel"
        if dimension == "region":
            calls["region"] += 1
            if fail_first_region and calls["region"] == 1:
                return tools.ToolResult(ok=False, tool="execute_sql", error="temporary")
        return tools.ToolResult(ok=True, tool="execute_sql", data={
            "sql_final": sql,
            "columns": ["dimension", "count"],
            "rows": [[dimension, 10]],
            "row_count": 1,
            "as_of": "2026-09-25T00:00:00+08:00",
        })

    monkeypatch.setattr(tools, "execute_sql", execute)


def test_router_keeps_simple_queries_fast_and_routes_complex_queries():
    assert decide_route("本月订单有多少").route == "single"
    assert decide_route("分析订单下降原因，分别看渠道和地区").route == "multi"
    assert decide_route("任意问题", requested_mode="multi").reason == "request_forced_multi"
    assert decide_route("任意问题", source_count=2).reason == "multiple_sources"


def test_parallel_reducer_merges_worker_artifacts_by_id():
    assert merge_by_id({"a": {"value": 1}}, {"b": {"value": 2}}) == {
        "a": {"value": 1}, "b": {"value": 2}}
    assert merge_by_id({"a": {"value": 1}}, {"a": {"value": 3}})["a"]["value"] == 3


def test_supervisor_graph_fans_out_verifies_and_binds_claims(cfg, monkeypatch):
    _patch_tools(monkeypatch)
    deps = _deps(cfg)
    state = initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id="run1", thread_id="thread1", org_id=65, source_id="builtin",
        max_workers=3, max_repair_rounds=1,
    )

    result = build_graph().invoke(
        state, {"configurable": {"thread_id": "thread1", "deps": deps}})

    assert result["status"] == "COMPLETED"
    assert len(result["subtasks_by_id"]) == 2
    assert len(result["evidence_by_id"]) == 2
    assert {row["verdict"] for row in result["reviews_by_id"].values()} == {"PASS"}
    evidence_ids = set(result["evidence_by_id"])
    assert all(set(claim["evidence_ids"]) == evidence_ids for claim in result["claims"])
    assert all(row["checksum"].startswith("sha256:")
               for row in result["evidence_by_id"].values())


def test_verifier_repairs_only_failed_worker_and_preserves_review_history(cfg, monkeypatch):
    _patch_tools(monkeypatch, fail_first_region=True)
    deps = _deps(cfg)
    state = initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id="run2", thread_id="thread2", org_id=65, source_id="builtin",
        max_workers=3, max_repair_rounds=1,
    )

    result = build_graph().invoke(
        state, {"configurable": {"thread_id": "thread2", "deps": deps}})

    assert result["status"] == "COMPLETED"
    assert len(result["evidence_by_id"]) == 2
    assert [row["verdict"] for row in result["reviews_by_id"].values()] == [
        "REPAIR", "PASS"]
    attempts = {task["title"]: task["attempt"]
                for task in result["subtasks_by_id"].values()}
    assert attempts == {"渠道": 1, "地区": 2}


def test_supervisor_graph_persists_protocol_state_for_cold_resume(cfg, monkeypatch):
    _patch_tools(monkeypatch)
    reset_graph()
    deps = _deps(cfg)
    state = initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id="persisted", thread_id="persisted", org_id=65,
        source_id="builtin", max_workers=3, max_repair_rounds=1,
    )
    graph = ensure_graph(cfg)
    graph.invoke(state, {"configurable": {"thread_id": "persisted", "deps": deps}})

    snapshot = graph.get_state({"configurable": {"thread_id": "persisted"}})
    assert snapshot.values["plan"]["plan_id"] == "persisted:plan"
    assert len(snapshot.values["evidence_by_id"]) == 2
    assert snapshot.values["skill_bindings_by_role"]
    assert not snapshot.next
    reset_graph()


def test_cancel_stops_workers_before_sql_execution(cfg, monkeypatch):
    execute_calls = {"count": 0}
    _patch_tools(monkeypatch)

    def should_not_execute(*args, **kwargs):
        execute_calls["count"] += 1
        raise AssertionError("canceled worker must not execute SQL")

    monkeypatch.setattr(tools, "execute_sql", should_not_execute)
    deps = _deps(cfg)
    deps.cancel_event = threading.Event()
    deps.cancel_event.set()
    state = initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id="canceled", thread_id="canceled", org_id=65,
        source_id="builtin", max_workers=3, max_repair_rounds=1,
    )

    result = build_graph().invoke(
        state, {"configurable": {"thread_id": "canceled", "deps": deps}})

    assert result["status"] == "CANCELED"
    assert not result["evidence_by_id"]
    assert execute_calls["count"] == 0


def test_budget_denies_all_model_calls_when_prompt_cannot_fit(cfg, monkeypatch):
    _patch_tools(monkeypatch)
    deps = _deps(cfg)
    state = initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id="budget", thread_id="budget", org_id=65, source_id="builtin",
        token_cap=1,
    )
    result = build_graph().invoke(
        state, {"configurable": {"thread_id": "budget", "deps": deps}})
    assert result["status"] == "BUDGET_EXCEEDED"
    assert result["tok_used"] == 0
    assert not result["answer"] and not result["evidence_by_id"]
    assert not deps.tracer.tok_in and not deps.tracer.tok_out


def test_parallel_worker_tokens_are_merged_in_checkpoint(cfg, monkeypatch):
    _patch_tools(monkeypatch)
    deps = _deps(cfg)
    result = build_graph().invoke(initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id="tokens", thread_id="tokens", org_id=65, source_id="builtin",
    ), {"configurable": {"thread_id": "tokens", "deps": deps}})
    usage = result["tok_by_actor"]
    assert len([key for key in usage if ":attempt:" in key]) == 2
    assert result["tok_used"] == sum(usage.values())
    assert result["cost_used_cny"] == sum(result["cost_by_actor"].values())


def test_currency_budget_denies_model_call_before_spending(cfg, monkeypatch):
    _patch_tools(monkeypatch)
    deps = _deps(cfg)
    result = build_graph().invoke(initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id="cost", thread_id="cost", org_id=65, source_id="builtin",
        cost_cap_cny=0.000001,
    ), {"configurable": {"thread_id": "cost", "deps": deps}})
    assert result["status"] == "BUDGET_EXCEEDED"
    assert result["cost_used_cny"] == 0
    assert not result["evidence_by_id"]


def test_sigkill_is_detected_as_interrupted_then_resumes_same_checkpoint(
        cfg, monkeypatch):
    from askdb import audit
    from askdb.multiagent import runtime
    from askdb.trace import write_audit

    _patch_tools(monkeypatch)
    thread_id = "a1b2c3d4e5f6"
    state = initial_state(
        question="分析订单下降原因，分别看渠道和地区",
        run_id=thread_id, thread_id=thread_id, org_id=65, source_id="builtin",
        max_workers=3, max_repair_rounds=1,
    )
    write_audit(cfg, {
        "trace_id": thread_id, "thread_id": thread_id,
        "ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "kind": "ask", "phase": "started", "execution_mode": "multi",
        "source": "builtin", "question": state["question"], "user": "",
    })
    ctx = multiprocessing.get_context("fork")
    entered = ctx.Event()
    child = ctx.Process(target=_run_until_killed, args=(cfg, state, entered))
    child.start()
    try:
        assert entered.wait(20), "Worker did not start before kill"
        # The worker may enter SQL before the asynchronous SQLite saver commits
        # the preceding superstep. Wait for the durable resume point first.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reset_graph()
            durable = ensure_graph(cfg).get_state(
                {"configurable": {"thread_id": thread_id}})
            if durable.next and durable.values.get("plan"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("no durable Worker checkpoint before kill")
        os.kill(child.pid, signal.SIGKILL)
        child.join(timeout=10)
        assert child.exitcode == -signal.SIGKILL

        reset_graph()  # cold process: discard the graph and its in-memory state
        snapshot = ensure_graph(cfg).get_state(
            {"configurable": {"thread_id": thread_id}})
        assert snapshot.next and snapshot.values["plan"], (
            snapshot.values.get("status"), snapshot.next, snapshot.tasks)
        assert runtime.is_resumable(thread_id, cfg) is True
        assert audit.tasks(cfg.audit_log, stale_after_s=1)[0]["status"] == "interrupted"

        class FakeLlm(_CoordinatorLlm, _WorkerLlm):
            def __init__(self, _cfg):
                pass

        monkeypatch.setattr(runtime, "LlmClient", FakeLlm)
        monkeypatch.setattr("askdb.multiagent.supervisor_graph.LlmClient", FakeLlm)
        from fastapi.testclient import TestClient
        from askdb import server

        monkeypatch.setattr(server, "load", lambda _path: cfg)
        response = TestClient(server.create_app("ignored.yaml")).post(
            "/api/resume", json={"thread_id": thread_id})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True and body["execution_mode"] == "multi"
        assert len(body["evidence"]) == 2
        assert all(task["attempt"] == 1 for task in body["plan"]["subtasks"])
        assert runtime.is_resumable(thread_id, cfg) is False
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=10)
        reset_graph()
