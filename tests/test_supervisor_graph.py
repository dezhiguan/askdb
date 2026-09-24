from __future__ import annotations

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
)
from askdb.trace import Tracer


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
