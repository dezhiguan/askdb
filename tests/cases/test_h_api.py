"""H 域 · HTTP 接口（18 条）。护栏拒绝是 200 + ok:false，不是 5xx。"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from askdb import server


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


def test_h01_index_no_store(client):
    r = client.get("/")
    assert r.status_code == 200 and "no-store" in r.headers.get("cache-control", "")


def test_h02_health_fields(client):
    d = client.get("/api/health").json()
    for k in ("datasource", "llm", "tenant", "guard", "config"):
        assert k in d, f"health 缺字段 {k}"


def test_h03_datasource_down_reported(cfg, monkeypatch, tmp_path):
    cfg.raw["datasource"]["path"] = str(tmp_path / "gone.duckdb")
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    c = TestClient(server.create_app("ignored.yaml"))
    d = c.get("/api/health").json()
    assert d["datasource"]["ok"] is False and d["datasource"]["hint"]


def test_h04_password_never_leaked(client, cfg):
    cfg.raw["datasource"]["password_env"] = "X"
    body = client.get("/api/health").text + client.get("/api/schema").text
    assert "password=" not in body


def test_h05_schema_endpoint(client):
    d = client.get("/api/schema").json()
    assert d["tables"] and "metrics" in d


def test_h06_selfcheck(client):
    d = client.get("/api/selfcheck").json()
    assert "checks" in d and any("写操作" in c["name"] for c in d["checks"])


def test_h07_introspect_lists_all(client):
    d = client.get("/api/introspect").json()
    assert d["ok"] and d["total"] >= d["allowed_count"]


def test_h08_introspect_tenant_mode(client):
    d = client.get("/api/introspect").json()
    for t in d["tables"]:
        if t.get("allowed"):
            assert t["tenant_mode"] in ("column", "filter", "exempt")


def test_h09_ask_ok(client, monkeypatch):
    from askdb import graph
    from askdb.graph import AskResult
    monkeypatch.setattr(graph, "ask", lambda *a, **k: AskResult(
        ok=True, question="q", trace_id="t", org_id=65, row_count=1))
    monkeypatch.setattr(server, "run_ask", lambda *a, **k: AskResult(
        ok=True, question="q", trace_id="t", org_id=65, row_count=1))
    d = client.post("/api/ask", json={"question": "有多少文档"}).json()
    assert d["ok"] and d["trace_id"]


def test_h10_ask_guard_reject_is_200(client):
    r = client.post("/api/sql", json={"sql": "DELETE FROM documents"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["rejected_by"] and r.json()["hint"]


@pytest.mark.parametrize("cid,payload", [
    ("H-11", {"question": ""}),
    ("H-12", {"question": "文" * 501}),
    ("H-13", {}),
])
def test_h11_h13_input_validation(cid, payload, client):
    r = client.post("/api/ask", json=payload)
    assert r.status_code == 422, f"{cid}: 应为 422，实际 {r.status_code}"


def test_h14_sql_direct_ok(client):
    d = client.post("/api/sql", json={"sql": "SELECT id AS x FROM documents"}).json()
    assert d["ok"] and d["row_count"] >= 0


def test_h15_sql_direct_rejected(client):
    d = client.post("/api/sql", json={"sql": "SELECT COUNT(*) AS n FROM chunks"}).json()
    assert d["ok"] is False and d["rejected_by"] == "R-03"


def test_h16_org_switch_changes_result(client):
    a = client.post("/api/sql", json={"sql": "SELECT COUNT(*) AS n FROM documents",
                                      "org_id": 65}).json()
    b = client.post("/api/sql", json={"sql": "SELECT COUNT(*) AS n FROM documents",
                                      "org_id": 66}).json()
    assert a["rows"] != b["rows"], "切换租户结果必须变化，否则隔离没生效"


def test_h17_cross_tenant_is_bounded(client):
    """按设计允许调用方传 org_id，但结果必须严格限定在该租户。"""
    d = client.post("/api/sql", json={"sql": "SELECT COUNT(*) AS n FROM documents",
                                      "org_id": 66}).json()
    assert d["ok"] and "org_id = 66" in d["sql_final"].replace('"', "")


def test_h18_eval_provenance(client):
    d = client.get("/api/eval").json()
    if d.get("available"):
        assert "provenance" in d and "failures" in d


def test_h19_health_ok_true_when_llm_intentionally_disabled(cfg, monkeypatch):
    """有意不接模型的实例，health 顶层 ok 必须为 true。

    对外开放实例故意不配密钥（config/public.yaml），此前顶层
    ok = db_ok and api_key()，于是线上 health 恒报 ok:false ——
    数据源明明是好的，看的人（包括我自己排查时）都以为部署失败了。
    """
    cfg.raw["llm"]["disabled"] = True
    monkeypatch.delenv(cfg.llm["api_key_env"], raising=False)
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    c = TestClient(server.create_app("ignored.yaml"))
    d = c.get("/api/health").json()
    assert d["datasource"]["ok"] is True
    assert d["llm"]["ok"] is False and d["llm"]["disabled"] is True
    assert d["ok"] is True, "有意禁用模型不是故障状态"


def test_h20_eval_picks_result_set_matching_current_datasource(cfg, tmp_path, monkeypatch):
    """同一份代码会部署成多个实例（对外连样例库、内部连生产库），
    评测页必须自动挑**与当前数据源匹配**的那套结果，而不是写死优先某一套。

    另钉住一处形状差异：blind 结果顶层直接是报告字段（n 是 int），
    ablation 顶层是 {组名: 报告}。取出处时不判类型就会对着 int 调 .get。
    """
    import json

    res = tmp_path / "evals" / "results"
    res.mkdir(parents=True)
    cfg.root = tmp_path
    # 一套跑在别的库上，一套跑在当前库上
    (res / "ragforge-blind.json").write_text(json.dumps({
        "group": "x", "n": 1, "accuracy": 0.9, "outcomes": [],
        "provenance": {"datasource": "postgresql:other@1.2.3.4:5432"}}), encoding="utf-8")
    (res / "blind.json").write_text(json.dumps({
        "group": "y", "n": 2, "accuracy": 0.5, "outcomes": [],
        "provenance": {"datasource": f"duckdb:{cfg.db_path.name}"}}), encoding="utf-8")

    monkeypatch.setattr(server, "load", lambda _p: cfg)
    d = TestClient(server.create_app("ignored.yaml")).get("/api/eval").json()
    assert d["available"] and d["provenance"]["matches_current"] is True
    assert d["blind"]["n"] == 2, "应选中与当前数据源匹配的那套"

    # 只有 ablation（顶层是 {组名: 报告}）时也不能崩
    (res / "blind.json").unlink()
    (res / "ablation2.json").write_text(json.dumps({
        "A": {"group": "A 基线", "n": 3, "accuracy": 0.4, "false_reject": 0.0,
              "cost_cny": 0.1, "p95_ms": 100, "outcomes": [],
              "provenance": {"datasource": f"duckdb:{cfg.db_path.name}"}}}), encoding="utf-8")
    d2 = TestClient(server.create_app("ignored.yaml")).get("/api/eval").json()
    assert d2["available"] and d2["groups"][0]["key"] == "A"
