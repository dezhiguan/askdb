"""HTTP 接口测试。

关键约定：**护栏拦截返回 200 + ok:false**，不是 5xx。
只有服务本身坏了才用非 2xx —— 前端据此区分"你写错了"和"我坏了"。
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from askdb import server
from askdb.server import WEB, WEB_LEGACY
from askdb.llm import LlmUsage, SqlDraft


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    return TestClient(server.create_app("ignored.yaml"))


def test_index_serves_page(client):
    """首页现在是前端工程的构建产物，页面主体在带 hash 的 bundle 里。

    仍然钉住「页面自报家门」：同一台机器上会同时跑多个实例，
    拿不准眼前这个是谁，排查就无从下手。
    """
    r = client.get("/")
    assert r.status_code == 200
    assert 'name="application-name" content="askdb"' in r.text
    assert '<div id="root"></div>' in r.text


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


def test_step_count_is_a_scalar_not_the_trace_array(client):
    """「执行步数」曾渲染成 [object Object],[object Object] —— 页面取错了字段。

    d.steps 是步骤追踪数组（对象列表），非空即为真；写成 `d.steps || d.step_count`
    会短路成整个数组。这里同时钉住两侧：接口给的 step_count 必须是标量，
    页面必须从 step_count 取值而不是 steps。
    """
    d = client.post("/api/ask", json={"question": "有多少文档"}).json()
    assert isinstance(d["step_count"], int)
    assert isinstance(d["steps"], list)          # 追踪数组，不是步数

    page = (WEB_LEGACY / "index.html").read_text(encoding="utf-8")
    assert 'd.steps || d.step_count' not in page
    assert '$("mSteps").textContent = (d.step_count' in page


def test_datasource_label_shows_real_host_not_tunnel(cfg):
    """经 SSH 隧道时，界面与评测出处必须显示隧道背后的真实库地址。

    dsn 里写的是本地转发端口（127.0.0.1:15432）—— 那是运维细节，
    不是数据源身份。出处栏照搬它，读的人会以为数据来自本机，
    而这一栏存在的全部意义就是说清"这组数字出自哪个库"。
    """
    dsn = "host=127.0.0.1 port=15432 dbname=ragforge user=askdb_ro"

    lbl = server._dsn_label(dsn, "8.163.30.216:5432")
    assert "8.163.30.216:5432" in lbl
    assert "经隧道" in lbl and "127.0.0.1:15432" in lbl   # 隧道端点仍要留着，排查时用得上

    # 未声明 upstream（直连）时保持原样，不臆造
    assert server._dsn_label(dsn) == "ragforge @ 127.0.0.1:15432"

    # 密码永远不出现
    assert "secret" not in server._dsn_label(dsn + " password=secret", "8.163.30.216:5432")


def test_eval_reports_paired_deltas_not_just_absolutes(client):
    """消融图画的是相对基线的**配对增量**，不是绝对准确率。

    各组跑的是完全相同的题目（§6.4 第 3 条），是配对设计 ——
    配对区间比两条独立区间灵敏得多，且"跨没跨过 0"直接回答
    "这个差异说明得了问题吗"，而柱长回答不了。
    """
    from askdb.server import _paired_delta, _by_category

    base = [{"id": f"q{i}", "category": "single", "passed": i < 5} for i in range(10)]
    # 与 base 同题：前 5 题保持对，后 5 题全部翻成对
    better = [{"id": f"q{i}", "category": "single", "passed": True} for i in range(10)]
    v = _paired_delta(base, better)
    assert v["improved"] == 5 and v["regressed"] == 0
    assert v["delta"] == 0.5 and v["lo"] > 0        # 全翻好，区间应完全在 0 右侧

    same = _paired_delta(base, base)
    assert same["delta"] == 0 and same["p"] == 1.0

    # 应拒题不计入准确率，也不该进配对统计
    with_reject = base + [{"id": "r1", "category": "reject", "passed": False}]
    assert _paired_delta(with_reject, with_reject)["n"] == 10

    cats = _by_category(with_reject)
    assert cats["single"] == [5, 10] and "reject" not in cats


def test_introspect_distinguishes_indirect_tenant_isolation(client):
    """接入页必须分清「直接有租户列」与「经关联间接归属」。

    documents 没有 org_id，靠 tenant_filter 经 kb_id 关联到
    knowledge_bases.org_id。此前该列只读 tenant_column，于是显示为空 ——
    读起来是"这张表没有租户隔离"，而这是整页最要害的一列。
    """
    d = client.get("/api/introspect").json()
    by = {t["name"]: t for t in d["tables"] if t.get("allowed")}
    assert by, "白名单应至少有一张表"
    for t in by.values():
        # 白名单内的表一律不得报成"无隔离"——配置层本就 fail-closed
        assert t["tenant_mode"] in ("column", "filter", "exempt")
        if t["tenant_mode"] in ("column", "filter"):
            assert t["tenant_via"], f"{t['name']} 应说明经哪一列隔离"


def test_eval_exposes_failure_traces_and_replay_config(client):
    """评测页写着「每条失败都带 trace_id，可从检查点原样复现」——
    那就必须真的把 trace_id 和复现所需的配置给出来。

    此前只有汇总数（"链路失败 4 · 结果不一致 3"），既不列 trace_id 也没有入口，
    等于告诉用户有这个能力却不给用它的路径。
    """
    d = client.get("/api/eval").json()
    if not d.get("available"):
        pytest.skip("本环境没有评测结果")
    assert "failures" in d
    for f in d["failures"]:
        assert f["trace_id"], f"{f['id']} 缺 trace_id，复现无从谈起"
        assert f["question"], f"{f['id']} 缺题面，光有编号看不出失败在哪"
    if d["failures"]:
        # 检查点库跟着配置走，命令里少了 -c 就会报"检查点里没有这个 trace"
        assert d["replay_config"], "必须给出复现该用哪份配置"


def test_health_reports_config_path(client):
    """提问页的复现命令要带 -c，配置路径得从 health 拿。"""
    assert "config" in client.get("/api/health").json()


def test_schema_endpoint_is_scoped_to_the_caller(cfg, monkeypatch):
    """/api/schema 必须按角色收窄。

    用未收窄的配置，会让人看到自己查不了的表连同全部字段 —— 实测 public.yaml
    下匿名角色只能查 knowledge_bases / orgs，这个接口却把 documents 与
    model_usage 的字段一起吐出来。既是信息泄露，也会让业务口径页列出一批
    用了就被 R-03 拦的口径。
    """
    from pathlib import Path

    from askdb.config import load

    root = Path(__file__).resolve().parent.parent
    public = load(root / "config" / "public.yaml")
    monkeypatch.setattr(server, "load", lambda _p: public)
    c = TestClient(server.create_app("ignored.yaml"))

    seen = {t["name"] for t in c.get("/api/schema").json()["tables"]}
    allowed = set(public.raw["role_policies"]["ANONYMOUS"]["tables"])
    assert seen <= allowed, f"匿名看到了不可查的表：{sorted(seen - allowed)}"

    # 口径引用的表不可见时整条摘掉，否则模型会照它写出被 R-03 拦下的 SQL
    for m in c.get("/api/schema").json()["metrics"]:
        assert set(m["scope"]) <= seen, f"口径「{m['name']}」引用了不可见的表"
