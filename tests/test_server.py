"""HTTP 接口测试。

关键约定：**护栏拦截返回 200 + ok:false**，不是 5xx。
只有服务本身坏了才用非 2xx —— 前端据此区分"你写错了"和"我坏了"。
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from askdb import server
from askdb.llm import LlmUsage, SqlDraft


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


def test_index_serves_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "askdb" in r.text


def test_health_reports_datasource_and_llm(client, cfg, monkeypatch):
    monkeypatch.delenv(cfg.llm["api_key_env"], raising=False)
    d = client.get("/api/health").json()
    assert d["datasource"]["ok"] is True
    assert d["llm"]["ok"] is False and d["llm"]["env"]
    assert d["tenant"]["column"] == "org_id"
    assert d["guard"]["max_rows"] == cfg.max_rows
    assert d["ok"] is False          # 缺密钥即整体未就绪


def test_health_flags_missing_datasource(cfg, tmp_path, monkeypatch):
    broken = copy.deepcopy(cfg)
    broken.raw = copy.deepcopy(cfg.raw)
    broken.raw["datasource"]["path"] = str(tmp_path / "gone.duckdb")
    monkeypatch.setattr(server, "load", lambda _p: broken)
    d = TestClient(server.create_app("x")).get("/api/health").json()
    assert d["datasource"]["ok"] is False
    assert "data.seed" in d["datasource"]["hint"]


def test_schema_exposes_tables_and_metrics(client):
    d = client.get("/api/schema").json()
    names = [t["name"] for t in d["tables"]]
    assert "documents" in names
    doc = next(t for t in d["tables"] if t["name"] == "documents")
    assert doc["tenant_column"] == "org_id"
    assert any(c["tenant"] for c in doc["columns"])
    assert any(c["enum"] for c in doc["columns"])
    assert d["metrics"] and d["metrics"][0]["definition"]


def test_selfcheck_endpoint(client):
    d = client.get("/api/selfcheck").json()
    assert d["ok"] is True
    assert any(c["name"] == "写操作实探" for c in d["checks"])


# ---------------------------------------------------------------- /api/sql

def test_sql_happy_path(client):
    d = client.post("/api/sql", json={"sql": "SELECT file_name FROM documents WHERE status='PROCESSING'"}).json()
    assert d["ok"] and d["row_count"] >= 0
    assert "org_id = 65" in d["sql_final"].replace("\n", " ")
    assert d["rewrites"] and d["cost_cny"] == 0.0
    assert [s["step"] for s in d["steps"]] == ["guard", "dry_run", "execute"]


def test_sql_blocked_returns_200_with_reason_and_hint(client):
    r = client.post("/api/sql", json={"sql": "DELETE FROM documents"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and d["rejected_by"] == "R-02"
    assert d["hint"] and "token" in d["hint"]
    assert d["steps"][0]["status"] == "blocked"


def test_sql_blocked_by_scan_threshold(cfg, monkeypatch):
    cfg.raw["guard"]["max_scan_rows"] = 1
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    d = TestClient(server.create_app("x")).post(
        "/api/sql", json={"sql": "SELECT file_name FROM documents"}).json()
    assert d["ok"] is False and d["rejected_by"] == "R-11"
    assert d["sql_final"] and "筛选条件" in d["hint"]


def test_sql_respects_org_override(client):
    d = client.post("/api/sql", json={"sql": "SELECT id FROM documents", "org_id": 66}).json()
    assert d["org_id"] == 66 and "org_id = 66" in d["sql_final"].replace("\n", " ")


def test_sql_rejects_empty_body(client):
    assert client.post("/api/sql", json={"sql": ""}).status_code == 422


# ---------------------------------------------------------------- /api/ask

def test_ask_uses_pipeline(client, monkeypatch):
    class Fake:
        def generate_sql(self, *a, **k):
            return SqlDraft(sql="SELECT file_name AS 文件名 FROM documents", reasoning="r"), LlmUsage(10, 5)

        def structured(self, schema, system, human):
            from askdb.planner import Assessment, Plan
            if schema is Plan:
                return Plan(multi_step=False, reason="替身"), LlmUsage(1, 1)
            return Assessment(enough=True, reason="替身"), LlmUsage(1, 1)


    monkeypatch.setattr(server, "run_ask",
                        lambda q, cfg, org_id=None: __import__("askdb.graph", fromlist=["ask"])
                        .ask(q, cfg, org_id=org_id, llm=Fake()))
    d = client.post("/api/ask", json={"question": "有哪些文档"}).json()
    assert d["ok"] and d["columns"] == ["文件名"]
    assert d["tables_hit"]


def test_ask_rejects_too_long_question(client):
    assert client.post("/api/ask", json={"question": "x" * 600}).status_code == 422


def test_ask_rejects_empty_question(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


# ---------------------------------------------------------------- MCP

def _mcp_call(mcp, name, args):
    """取出工具返回的文本负载。"""
    import json

    import anyio

    res = anyio.run(lambda: mcp.call_tool(name, args))
    blocks = getattr(res, "content", None) or []
    return json.loads(blocks[0].text)


def test_mcp_exposes_three_tools(cfg):
    import anyio

    from askdb.mcp_server import build_server

    tools = anyio.run(build_server(cfg).list_tools)
    assert {t.name for t in tools} == {"ask", "run_sql", "schema"}
    assert all(t.description for t in tools)


def test_mcp_run_sql_applies_guard(cfg):
    """护栏不因调用方是 Agent 而放松 —— 自动发起的查询更没人盯着。"""
    from askdb.mcp_server import build_server

    p = _mcp_call(build_server(cfg), "run_sql", {"sql": "DELETE FROM documents"})
    assert p["ok"] is False and p["rejected_by"] == "R-02"


def test_mcp_run_sql_injects_tenant(cfg):
    from askdb.mcp_server import build_server

    p = _mcp_call(build_server(cfg), "run_sql", {"sql": "SELECT id FROM documents"})
    assert p["ok"] and "org_id = 65" in p["sql"].replace("\n", " ")


def test_mcp_run_sql_respects_org_override(cfg):
    from askdb.mcp_server import build_server

    p = _mcp_call(build_server(cfg), "run_sql",
                  {"sql": "SELECT id FROM documents", "org_id": 66})
    assert p["ok"] and "org_id = 66" in p["sql"].replace("\n", " ")


def test_mcp_schema_exposes_metrics_and_limits(cfg):
    from askdb.mcp_server import build_server

    p = _mcp_call(build_server(cfg), "schema", {})
    assert p["tables"] and p["metrics"]
    assert p["limits"]["max_rows"] == cfg.max_rows
    assert "强制注入" in p["tenant"]["note"]


def test_mcp_ask_payload_carries_sql_and_caveat(cfg, monkeypatch):
    """结果必须与 SQL 一并回传 —— 这是唯一的正确性兜底。"""
    from askdb import graph, mcp_server
    from askdb.planner import Assessment, Plan

    class Fake:
        def generate_sql(self, *a, **k):
            return SqlDraft(sql="SELECT file_name AS 文件名 FROM documents",
                            reasoning="r"), LlmUsage(5, 2)

        def structured(self, schema, system, human):
            if schema is Plan:
                return Plan(multi_step=False, reason="替身"), LlmUsage(1, 1)
            return Assessment(enough=True, reason="替身"), LlmUsage(1, 1)

    monkeypatch.setattr(mcp_server, "run_ask",
                        lambda q, c, org_id=None: graph.ask(q, c, org_id=org_id, llm=Fake()))
    p = _mcp_call(build := mcp_server.build_server(cfg), "ask", {"question": "有哪些文档"})
    assert p["ok"] and p["sql"] and p["rewrites"]      # 强制改写要让调用方看见
    assert "人工核对" in p["caveat"]
    assert p["trace_id"]


# ---------------------------------------------------------------- /api/eval

def test_eval_reports_unavailable_without_results(cfg, tmp_path, monkeypatch):
    """没有结果文件时如实说"尚未运行"，不编数字。"""
    import copy

    broken = copy.copy(cfg)
    broken.root = tmp_path                      # 指向没有 evals/results 的目录
    monkeypatch.setattr(server, "load", lambda _p: broken)
    d = TestClient(server.create_app("x")).get("/api/eval").json()
    assert d["available"] is False


def test_eval_exposes_real_results(cfg, tmp_path, monkeypatch):
    import copy
    import json

    res = tmp_path / "evals" / "results"
    res.mkdir(parents=True)
    (res / "blind.json").write_text(json.dumps({
        "n": 18, "accuracy": 0.5, "false_reject": 0.062, "block_rate": 0.5,
        "multi_misuse": 0.0, "p95_ms": 22894, "cost_cny": 0.0599,
        "failure_kinds": {"链路失败": 4}}), encoding="utf-8")
    (res / "ablation2.json").write_text(json.dumps({
        "A": {"group": "A 裸 Prompt", "n": 40, "accuracy": 0.595, "false_reject": 0.027,
              "cost_cny": 0.118, "p95_ms": 6754},
        "E": {"group": "E 旧值", "n": 40, "accuracy": 0.1, "false_reject": 0.0,
              "cost_cny": 0.1, "p95_ms": 1}}), encoding="utf-8")
    (res / "ablation_F.json").write_text(json.dumps({
        "E": {"group": "E 干跑阈值", "n": 40, "accuracy": 0.73, "false_reject": 0.0,
              "cost_cny": 0.1093, "p95_ms": 7104}}), encoding="utf-8")

    c = copy.copy(cfg)
    c.root = tmp_path
    monkeypatch.setattr(server, "load", lambda _p: c)
    d = TestClient(server.create_app("x")).get("/api/eval").json()
    assert d["available"] and d["blind"]["accuracy"] == 0.5
    by = {g["key"]: g for g in d["groups"]}
    # E 必须取配额修复后的重跑值，而不是被污染的那一轮
    assert by["E"]["accuracy"] == 0.73 and by["E"]["rerun"] is True
    assert by["A"]["rerun"] is False
    assert d["shipped"] == "E"


def test_sql_endpoint_renders_decimal_readably(client):
    """PostgreSQL 的高标度 numeric 用 str() 会变成 0E-20，人认不出那是 0。

    /api/ask 与 /api/sql 必须走同一套值渲染，否则同一个值两条路径显示不一致。
    """
    from askdb.graph import jsonable
    from decimal import Decimal

    assert jsonable(Decimal("0E-20")) == "0"
    assert jsonable(Decimal("1.500")) == "1.5"
    assert jsonable(Decimal("1000")) == "1000"

    d = client.post("/api/sql", json={
        "sql": "SELECT COUNT(*) FILTER (WHERE status='NOPE') * 1.0 "
               "/ NULLIF(COUNT(*), 0) AS 比率 FROM documents"}).json()
    assert d["ok"] and "E-" not in str(d["rows"][0][0])
