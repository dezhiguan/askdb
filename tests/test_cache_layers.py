"""多级缓存 L0-L3 的行为护栏（2026-09-15）。

每条都对着一个真实断过或可能静默断掉的地方：D-6 交接路径不写缓存、
改了脱敏配置旧答案照旧返回、L3 拿旧文字配新数据。它们是回归护栏，
不是覆盖率填充。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from askdb import auth, l0, qcache, semcache, server

SECRET = "s" * 40


class _MemCache:
    enabled = True
    ttl = 600

    def __init__(self):
        self.store = {}
        self.locks = set()
        self.leads = 0

    def get(self, k):
        return self.store.get(k)

    def put(self, k, v, ttl):
        self.store[k] = v

    def lead(self, k, wait_ms=0):
        self.leads += 1
        if k in self.locks:
            return self.store.get(k)
        self.locks.add(k)
        return None

    def release(self, k):
        self.locks.discard(k)

    def stats(self):
        return {"enabled": True, "hits": 0, "misses": 0}


@pytest.fixture
def ccfg(cfg, monkeypatch):
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, SECRET)
    cfg.raw["auth"] = {"enabled": True, "required": False, "accounts": []}
    cfg.raw["agent"] = {**cfg.raw.get("agent", {}), "async_after_ms": 0}
    cfg.raw["semantic_cache"] = {"mode": "shadow"}
    return cfg


@pytest.fixture
def cclient(ccfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: ccfg)
    return TestClient(server.create_app("ignored.yaml"))


def _ok(question, cfg, org_id=None, **kw):
    from askdb.graph import AskResult
    return AskResult(ok=True, question=question, trace_id=kw["trace_id"],
                     org_id=0, thread_id=kw["thread_id"],
                     reasoning="共 1,234,567 行", sql_final="SELECT COUNT(*) FROM skus",
                     columns=["n"], rows=[[1234567]], row_count=1,
                     cost_cny=0.0051, elapsed_ms=19840)


# ---------------------------------------------------------------- L1
def test_detached_run_enters_the_answer_cache(cclient, monkeypatch):
    """D-6：交接出去的执行也要进缓存，第二次同样的提问同步直返。"""
    mem = _MemCache()
    monkeypatch.setattr(server, "build_answer_cache", lambda _c: mem)
    monkeypatch.setattr(server, "run_agent", _ok)

    body = cclient.post("/api/ask", json={"question": "商品总共有多少条记录"}).json()
    assert body.get("async"), "async_after_ms=0 应当立即交接"
    for _ in range(60):
        if len(mem.store) >= 2:
            break
        time.sleep(0.05)

    again = cclient.post("/api/ask", json={"question": "商品总共有多少条记录"}).json()
    assert not again.get("async")
    assert again.get("cached") is True
    assert again.get("cost_cny") == 0.0
    # D-3：省下的钱要记下来，否则拿不出这一层的收益证明
    assert again.get("saved_cny") == pytest.approx(0.0051)
    assert again.get("saved_ms") == 19840


def test_blocked_result_never_enters_the_cache(cclient, monkeypatch):
    from askdb.graph import AskResult
    mem = _MemCache()
    monkeypatch.setattr(server, "build_answer_cache", lambda _c: mem)
    monkeypatch.setattr(
        server, "run_agent",
        lambda q, cfg, org_id=None, **kw: AskResult(
            ok=False, question=q, trace_id=kw["trace_id"], org_id=0,
            thread_id=kw["thread_id"], rejected_by="R-11",
            sql_final="SELECT * FROM skus", explain_rows=9_000_000))
    cclient.post("/api/ask", json={"question": "把商品全部列出来"})
    time.sleep(0.6)
    keys = [k for k in mem.store if not k.startswith("ho:")]
    assert not keys, "被 R-11 拦下的结果不该进缓存"


# ---------------------------------------------------------------- D-2
def test_key_changes_when_masking_config_changes(cfg):
    """脱敏列一改，key 必须跟着变 —— 否则旧的没打码结果还会被返回。"""
    before = qcache.scope(cfg)
    t = next(iter(cfg.tables.values()))
    col = next(iter(t.columns.values()))
    col.sensitive = not col.sensitive
    assert qcache.scope(cfg) != before

    k1 = qcache.make_key(question="q", source_id="s", org_id=1, role="R",
                         scope_fp="aaa")
    k2 = qcache.make_key(question="q", source_id="s", org_id=1, role="R",
                         scope_fp="bbb")
    assert k1 != k2


# ---------------------------------------------------------------- L0
def test_l0_caches_the_query_vector_not_the_index_build():
    l0.reset()
    calls = []
    l0.memo("embed", "m\x00问题", lambda: calls.append(1) or [0.1])
    l0.memo("embed", "m\x00问题", lambda: calls.append(1) or [0.9])
    assert len(calls) == 1
    assert l0.stats()["embed"]["hits"] == 1


def test_l0_prompt_key_ignores_per_request_values(cfg):
    """系统提示的 L0 key 只能由旋钮与工具集构成 —— 掺进随请求变的值，
    厂商侧那 91% 的前缀命中率会一起作废。"""
    from askdb.agent import render_agent_system
    l0.reset()
    a = render_agent_system(cfg, frozenset())
    b = render_agent_system(cfg, frozenset())
    assert a == b and l0.stats()["prompt"]["hits"] == 1
    c = render_agent_system(cfg, frozenset({"search_schema"}))
    assert c != a, "收起工具应当渲染出不同的提示词"


# ---------------------------------------------------------------- L2 / L3
def test_time_sensitive_questions_never_take_the_answer_lane():
    assert semcache._is_time_sensitive("今天新增了多少商品")
    assert not semcache._is_time_sensitive("商品总共有多少条记录")


def test_literal_dates_are_not_reusable_as_a_plan():
    assert semcache._plan_reusable("SELECT count(*) FROM skus")
    assert not semcache._plan_reusable(
        "SELECT count(*) FROM o WHERE d = DATE '2026-07-27'")
    assert not semcache._plan_reusable("SELECT 1 WHERE m = '2026-07'")


def test_backfill_substitutes_only_what_has_provenance():
    old = [["华为", 1200], ["腾讯", 980]]
    new = [["华为", 1350], ["腾讯", 980]]
    got = semcache._backfill("华为 1,200 条，腾讯 980 条，共 2 家。", old, new)
    assert got == "华为 1,350 条，腾讯 980 条，共 2 家。"
    # 结论里的数在结果行里找不到出处 → 不许命中
    assert semcache._backfill("共 2,180 条", old, new) is None
    # 分组键变了 → 不许命中
    assert semcache._backfill("x", [["a", 1]], [["b", 1]]) is None
    # 数据没变 → 原文逐字返回
    assert semcache._backfill("同上", old, old) == "同上"


def test_storable_gate_is_stricter_than_l1():
    base = {"ok": True, "rejected_by": None, "sql_final": "SELECT 1"}
    assert semcache._storable(base)
    for bad in ("truncated", "mask_degraded", "recall_blind",
                "recall_degraded", "scope_narrowed"):
        assert not semcache._storable({**base, bad: True}), bad
    assert not semcache._storable({**base, "ungrounded_numbers": ["9"]})


def test_shadow_mode_never_changes_the_response(cclient, monkeypatch):
    """影子档只记数，不改变任何返回 —— 这是它存在的全部意义。"""
    semcache.reset()
    mem = _MemCache()
    monkeypatch.setattr(server, "build_answer_cache", lambda _c: mem)
    monkeypatch.setattr(server, "run_agent", _ok)
    monkeypatch.setattr(semcache, "enabled", lambda _c: True)
    monkeypatch.setattr(semcache, "embed_question", lambda _c, _q: [0.1, 0.2])
    monkeypatch.setattr(
        semcache, "lookup",
        lambda *a, **k: semcache.Candidate("answer", 0.99, "近义问法",
                                           {"ok": True}, "SELECT 1", 3))
    monkeypatch.setattr(semcache, "remember", lambda *a, **k: None)

    body = cclient.post("/api/ask", json={"question": "商品有多少条"}).json()
    assert body.get("cached") is not True, "shadow 档不该真的用缓存作答"
    assert semcache.stats()["shadow"].get("would_answer") == 1


def test_enforce_mode_serves_the_semantic_hit(cclient, ccfg, monkeypatch):
    semcache.reset()
    ccfg.raw["semantic_cache"] = {"mode": "enforce"}
    mem = _MemCache()
    monkeypatch.setattr(server, "build_answer_cache", lambda _c: mem)
    monkeypatch.setattr(server, "run_agent", _ok)
    monkeypatch.setattr(semcache, "enabled", lambda _c: True)
    monkeypatch.setattr(semcache, "embed_question", lambda _c, _q: [0.1, 0.2])
    monkeypatch.setattr(
        semcache, "lookup",
        lambda *a, **k: semcache.Candidate(
            "answer", 0.99, "商品一共多少条",
            {"ok": True, "reasoning": "共 1,234,567 行", "cost_cny": 0.0051,
             "elapsed_ms": 19840, "trace_id": "abc123abc123"},
            "SELECT 1", 3))
    monkeypatch.setattr(semcache, "remember", lambda *a, **k: None)

    body = cclient.post("/api/ask", json={"question": "商品总数是多少"}).json()
    assert body.get("cached") is True
    assert body.get("cache_layer") == "L2"
    assert body.get("cost_cny") == 0.0
    assert body.get("saved_cny") == pytest.approx(0.0051)


def test_serve_plan_reruns_the_sql_through_the_guard(cfg, monkeypatch):
    """L3 的核心：复用的是 SQL，不是答案，而且**必须重新过闸**。"""
    from askdb import tools
    semcache.reset()
    seen = {}

    def _exec(sql, c, org_id, executor=None):
        seen["sql"] = sql
        return tools.ToolResult(ok=True, tool="execute_sql", data={
            "sql_final": sql, "columns": ["公司", "条数"],
            "rows": [["华为", 1350], ["腾讯", 980]], "row_count": 2,
            "truncated": False, "as_of": "2026-09-15", "masked_columns": [],
            "mask_degraded": False, "rules_fired": [], "rewrites": [],
            "explain_rows": 2000})

    monkeypatch.setattr(tools, "execute_sql", _exec)
    cand = semcache.Candidate(
        "plan", 0.9, "各公司条数",
        {"ok": True, "reasoning": "华为 1,200 条，腾讯 980 条。",
         "caliber": "按 company 分组计数",
         "rows": [["华为", 1200], ["腾讯", 980]], "columns": ["公司", "条数"],
         "row_count": 2, "masked_columns": ["x"], "truncated": True},
        "SELECT company, count(*) FROM t GROUP BY company", 7200)

    out = semcache.serve_plan(cfg, cand, org_id=0)
    assert seen["sql"] == cand.sql, "必须把上次那条 SQL 原样重新执行一遍"
    assert out["rows"] == [["华为", 1350], ["腾讯", 980]], "结果要取本次跑的"
    assert out["reasoning"] == "华为 1,350 条，腾讯 980 条。", "数值要回填"
    # 脱敏与截断描述的是**这一次**执行，沿用上次就是在说假话
    assert out["masked_columns"] == [] and out["truncated"] is False
    assert semcache.stats()["plan_served"] == 1


def test_serve_plan_falls_back_when_the_rerun_is_blocked(cfg, monkeypatch):
    from askdb import tools
    semcache.reset()
    monkeypatch.setattr(
        tools, "execute_sql",
        lambda *a, **k: tools.ToolResult(ok=False, tool="execute_sql",
                                         rejected_by="R-11", error="超阈值"))
    cand = semcache.Candidate("plan", 0.9, "q", {"ok": True}, "SELECT 1", 10)
    assert semcache.serve_plan(cfg, cand, org_id=0) is None
    assert semcache.stats()["plan_rerun_failed"] == 1


def test_serve_plan_falls_back_when_backfill_cannot_verify(cfg, monkeypatch):
    """回填对不上就判未命中，回落模型链路 —— 绝不拿旧文字配新数据。"""
    from askdb import tools
    semcache.reset()
    monkeypatch.setattr(tools, "execute_sql", lambda *a, **k: tools.ToolResult(
        ok=True, tool="execute_sql",
        data={"rows": [["华为", 1350]], "columns": ["公司", "条数"],
              "row_count": 1}))
    cand = semcache.Candidate(
        "plan", 0.9, "q",
        {"ok": True, "reasoning": "共 2 家", "rows": [["华为", 1200], ["腾讯", 980]]},
        "SELECT 1", 10)
    assert semcache.serve_plan(cfg, cand, org_id=0) is None
    assert semcache.stats()["plan_backfill_miss"] == 1


def test_shadow_verdict_compares_numbers_not_wording():
    """影子档要答的是"命中了会不会答错"，所以比数字集合、不比措辞。"""
    semcache.reset()
    c = semcache.Candidate("answer", 0.95, "q",
                           {"reasoning": "共 1,234,567 行"}, "SELECT 1", 5)
    assert semcache.note_shadow_verdict(
        c, {"reasoning": "一共 1234567 条记录"}) == "agree"
    assert semcache.note_shadow_verdict(
        c, {"reasoning": "共 9,999 行"}) == "disagree"
    assert semcache.note_shadow_verdict(c, {"reasoning": "查不到"}) == "unknown"
    sh = semcache.stats()["shadow"]
    assert sh["answer_agree"] == 1 and sh["answer_disagree"] == 1
