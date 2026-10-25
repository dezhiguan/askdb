"""The governed path is tested with the real SDK protocol and domain result types."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from askdb.keel_prompts import resolve, templates
from askdb.keel_runtime import Invocation, active

ROOT = Path(__file__).resolve().parents[1]


def test_prompt_copies_are_complete_and_match_runtime_rendering(cfg):
    from askdb.agent import render_agent_system
    from keel.prompts import Prompt
    from askdb.agent import _sys
    assert resolve(_sys(render_agent_system(cfg), cfg))[0] == "query-agent"
    for name, pattern, names, body in templates():
        assert (ROOT / "prompts" / f"{name}.md").read_text() == body
        prompt = Prompt("askdb/" + name, 3, "text", False, {}, body)
        values_in = {key: "\n\n【方法论 · 必须遵循】\nfixture" if key == "methodology" else "value-" + key for key in names}
        rendered = prompt.compile(**values_in)
        found, values = resolve(rendered)
        assert found == name
        assert values == values_in


def test_unregistered_prompt_cannot_bypass_management():
    with pytest.raises(ValueError, match="未登记"):
        resolve("some unregistered instructions")


class Draft(BaseModel):
    sql: str


@pytest.mark.parametrize("managed_config,configured,expected", [({}, 0, 0), ({}, 0.15, 0.15), ({"temperature": 0.35}, 0, 0.35)])
def test_structured_uses_ctx_model_prompt_and_schema_validation(cfg, managed_config, configured, expected):
    from askdb.llm import LlmClient
    from keel.llm.client import ChatResult, ToolCall
    from keel.prompts import Prompt

    async def execute():
        prompt = Prompt("askdb/fast", 2, "text", False, dict(managed_config), "managed instructions")
        ctx = SimpleNamespace(prompt=AsyncMock(return_value=prompt),
                              llm=SimpleNamespace(chat=AsyncMock(return_value=ChatResult(
                                  "", (ToolCall("call-1", "Draft", {"sql": "SELECT 1"}),)))))
        invocation = Invocation(ctx, asyncio.get_running_loop())
        binding = active.set(invocation)
        try:
            client = LlmClient(cfg, llm_cfg={**cfg.llm, "temperature": configured})
            # A platform invocation needs no vendor key, no legacy quota and no ChatOpenAI.
            client._build = lambda: pytest.fail("legacy model was used")
            client.quota.reserve = lambda: pytest.fail("legacy quota was used")
            system = next(body for name, _, _, body in templates() if name == "fast").replace("{{methodology}}", "")
            value, _ = await asyncio.to_thread(client.structured, Draft, system, "question")
            assert value.sql == "SELECT 1"
            kwargs = ctx.llm.chat.call_args.kwargs
            assert kwargs["prompt"].name == prompt.name and kwargs["prompt"].version == prompt.version
            assert kwargs["prompt"].config["temperature"] == expected
            assert prompt.config == managed_config
            assert kwargs["emit_tokens"] is False
            assert ctx.llm.chat.call_args.args[0][0]["content"] == "managed instructions"
            ctx.llm.chat.return_value = ChatResult("", (ToolCall("bad", "Draft", {}),))
            with pytest.raises(ValueError):
                await asyncio.to_thread(client.structured, Draft, system, "question")
        finally:
            active.reset(binding)
    asyncio.run(execute())


def test_parallel_invocations_keep_their_own_context_and_timeout():
    async def execute():
        async def call(label):
            ctx = SimpleNamespace()
            bridge = Invocation(ctx, asyncio.get_running_loop())
            binding = active.set(bridge)
            try:
                async def value():
                    await asyncio.sleep(.01)
                    return label
                return await asyncio.to_thread(lambda: active.get().call(value()))
            finally:
                active.reset(binding)
        assert await asyncio.gather(call("one"), call("two")) == ["one", "two"]
        bridge = Invocation(SimpleNamespace(), asyncio.get_running_loop())
        async def timed_out():
            raise TimeoutError("actual model timeout")
        with pytest.raises(TimeoutError, match="actual model"):
            await asyncio.wait_for(asyncio.to_thread(bridge.call, timed_out()), timeout=2)
    asyncio.run(execute())


@pytest.fixture
def governed(monkeypatch, tmp_path):
    values = {"KEEL_ENABLED": "1", "KEEL_ENV": "staging", "KEEL_MANIFEST": str(ROOT / "agent.yaml"),
              "KEEL_ASKDB_TOKEN": "test-bridge", "KEEL_LLM_BASE_URL": "http://model.invalid",
              "KEEL_LLM_KEY": "test-virtual", "KEEL_AUDIT_URL": "http://audit.invalid",
              "KEEL_AUDIT_TOKEN": "test-audit", "KEEL_AUDIT_SPOOL_PATH": str(tmp_path / "audit.jsonl")}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "KEEL_LANGFUSE_HOST",
                 "KEEL_LANGFUSE_PUBLIC_KEY", "KEEL_LANGFUSE_SECRET_KEY", "KEEL_MODE"):
        monkeypatch.delenv(name, raising=False)
    return values


def test_real_sdk_invoke_has_auth_environment_steps_and_result(governed, cfg, monkeypatch):
    from askdb.keel_app import mount
    from askdb.graph import AskResult
    from keel.audit.reporter import AuditReporter
    monkeypatch.setattr(AuditReporter, "record", AsyncMock())

    def query(text, query_cfg, **kwargs):
        assert active.get() is not None
        assert query_cfg.db_type == "duckdb"
        assert query_cfg.path == "config/keel-eval.yaml"
        from askdb.trace import StepTrace
        kwargs["on_span"](StepTrace("execute_sql", status="degraded"))
        return AskResult(True, text, kwargs["trace_id"], 65, reasoning="共 5 条", columns=["count"], rows=[[5]], row_count=1)
    monkeypatch.setattr("askdb.ask.run_ask", query)
    app = FastAPI()
    mount(app, cfg)
    from askdb.server import fallback_status
    assert cfg.llm["provider"] == "keel"
    assert cfg.llm["api_key_env"] == "KEEL_LLM_KEY"
    assert fallback_status(cfg)["key_present"] is True
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.post("/v1/invoke", json={"input": {"text": "count"}}).status_code == 401
        headers = {"X-Keel-Bridge": "test-bridge", "X-Keel-Env": "prod"}
        assert client.post("/v1/invoke", headers=headers, json={"input": {"text": "count"}}).status_code == 400
        headers.update({"X-Keel-Env": "staging", "X-Keel-Eval-Run": "eval-isolation"})
        response = client.post("/v1/invoke", headers=headers, json={"input": {"text": "count"}})
        assert response.status_code == 200, response.text
        assert "event: final" in response.text and "event: step" in response.text, response.text
        assert "count" in response.text and "5" in response.text
        assert "prompt_versions" in response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert any(event.get("name") == "execute_sql" and event.get("status") == "fallback" for event in events)


def test_startup_does_not_silently_remove_models(cfg, monkeypatch):
    from askdb.keel_app import mount
    monkeypatch.delenv("KEEL_ASKDB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="Keel requires"):
        mount(FastAPI(), cfg)


def test_platform_user_without_query_permission_is_denied(cfg, monkeypatch):
    from askdb.keel_app import invoke
    from keel.protocol.errors import KeelError
    ctx = SimpleNamespace(eval_run_id=None, user_id="not-a-member")
    monkeypatch.setattr("askdb.auth.roles_of", lambda *args: [])
    async def execute():
        with pytest.raises(KeelError, match="查询权限"):
            await invoke(SimpleNamespace(input={"text": "count"}), ctx, cfg)
    asyncio.run(execute())


def test_business_tool_goes_through_sdk_audit_and_keeps_sql_guard(cfg, monkeypatch):
    from askdb.keel_runtime import register_tools
    from askdb.tools import ToolContext, invoke as tool_invoke
    from keel.agent import Agent
    from keel.context import Context
    from keel.manifest import load_manifest

    async def execute():
        agent = Agent(load_manifest(ROOT / "agent.yaml"))
        register_tools(agent)
        audit = SimpleNamespace(record=AsyncMock(), aclose=AsyncMock())
        ctx = Context("askdb", "run", "trace", asyncio.Queue(), manifest=agent.manifest,
                      llm_client=SimpleNamespace(), audit_reporter=audit, env="staging", tool_functions=agent._tools)
        invocation = Invocation(ctx, asyncio.get_running_loop())
        binding = active.set(invocation)
        try:
            result = await asyncio.to_thread(tool_invoke, "execute_sql", {"sql": "DELETE FROM documents"}, ToolContext(cfg, 65))
            assert not result.ok and result.rejected_by
            assert audit.record.await_args.args[:3] == ("tool.call", "low", "allowed")
            assert audit.record.await_args.kwargs["resource"] == "execute_sql"
        finally:
            active.reset(binding)
            await ctx.aclose()
    asyncio.run(execute())


def test_scorer_requires_matching_rows_and_rejects_sql_or_swapped_counts():
    from evals.scorers import answer_correctness
    expected = {"cells": [["FAILED", "30"], ["COMPLETED", "70"]]}
    assert answer_correctness(expected, "SQL: SELECT 30, 70; FAILED COMPLETED", []) == 0
    assert answer_correctness(expected, "| FAILED | 70 |\n| COMPLETED | 30 |", []) == 0
    assert answer_correctness(expected, "| FAILED | 30 |\n| COMPLETED | 70 |", []) == 1


def test_native_sql_audit_uses_canonical_sdk_event_builder(governed, monkeypatch):
    from askdb import keel_runtime
    from askdb.keel_shadow import mirror_audit
    from keel.agent import Agent
    from keel.manifest import load_manifest
    audit = SimpleNamespace(record=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(keel_runtime, "_native_agent", Agent(load_manifest(ROOT / "agent.yaml")))
    monkeypatch.setattr("keel.audit.reporter.AuditReporter", lambda *args, **kwargs: audit)
    mirror_audit({"kind": "sql", "trace_id": "native-trace", "sql": "SELECT 1"})
    assert audit.record.await_args.args == ("legacy.sql", "low", "allowed")
    assert audit.record.await_args.kwargs["trace_id"] == "native-trace"
    audit.aclose.assert_awaited_once()


def test_native_gateway_and_semantic_trace_share_the_actual_otel_id(governed, cfg, monkeypatch):
    from askdb import keel_runtime
    from keel.agent import Agent
    from keel.manifest import load_manifest
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    provider = TracerProvider()
    monkeypatch.setattr(keel_runtime, "_native_tracer", provider.get_tracer("native-test"))
    monkeypatch.setattr(keel_runtime, "_native_agent", Agent(load_manifest(ROOT / "agent.yaml")))
    monkeypatch.setattr("keel.context.Context", lambda agent, run, trace_id, *args, **kwargs:
                        SimpleNamespace(trace_id=trace_id, aclose=AsyncMock()))

    def query(subject, config, **kwargs):
        actual = trace.get_current_span().get_span_context()
        assert actual.is_valid
        assert active.get().ctx.trace_id == f"{actual.trace_id:032x}"
        assert kwargs["trace_id"] == "legacy-query-id"
        return "ok"
    try:
        assert keel_runtime.run_native("question", cfg, operation=query, trace_id="legacy-query-id") == "ok"
    finally:
        provider.shutdown()


def test_contract_scorer_validates_typed_results_and_sql_rows():
    from evals.scorers import answer_correctness
    expected = {'checks': [{'path': 'execution.ok', 'equals': True},
                           {'path': 'execution.data.rows.0.0', 'equals': 94976}]}
    assert answer_correctness(expected, json.dumps({'execution': {'ok': True, 'data': {'rows': [[94976]]}}}), []) == 1
    assert answer_correctness(expected, json.dumps({'execution': {'ok': True, 'data': {'rows': [[1]]}}}), []) == 0
    assert answer_correctness(expected, '94976', []) == 0


def test_contract_input_is_fixed_whitelist_and_full_prompt_coverage():
    from askdb.keel_evaluation import CONTRACTS, parse
    from askdb.keel_prompts import DEFINITIONS
    assert set(CONTRACTS) | {'intent', 'query-agent'} == set(DEFINITIONS)
    assert parse('ordinary question') is None
    with pytest.raises(ValueError):
        parse(json.dumps({'keel_eval': 'structured-contract-v1', 'prompt': 'arbitrary', 'human': 'x'}))


def test_contract_numeric_scoring_handles_format_without_accepting_wrong_numbers():
    from evals.scorers import _contains
    assert _contains('共有 94,976 行', '94976')
    assert _contains('共有94976行', '94976')
    for value in ['194976', '949760', '-94976', '94976.1']:
        assert not _contains(value, '94976')


def test_dataset_archives_only_the_exact_builtin_protocol_scaffold():
    from evals.prepare_dataset import protocol_placeholder
    scaffold = {'input': 'probe', 'expectedOutput': 'probe ok', 'metadata': {'source': 'keel-builtin', 'case': 'protocol'}}
    assert protocol_placeholder(scaffold)
    assert not protocol_placeholder({**scaffold, 'metadata': {'source': 'user'}})
    assert not protocol_placeholder({**scaffold, 'input': 'real user question'})


def test_sdk_loads_only_the_three_argument_answer_scorer():
    from keel.eval.run import load_scorers
    scorers = load_scorers(ROOT / "evals/scorers.py")
    assert [scorer.__name__ for scorer in scorers] == ["answer_correctness"]
    expected = {"checks": [{"path": "parsed.answer", "contains": "94976"}]}
    assert [scorer(expected, json.dumps({"parsed": {"answer": "94,976行"}}), ["contract"]) for scorer in scorers] == [1.0]
    assert [scorer(expected, json.dumps({"parsed": {"answer": "194976行"}}), ["contract"]) for scorer in scorers] == [0.0]
