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
