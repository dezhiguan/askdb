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


# ------------------------------------------------ 语义层的确定性判据
def test_similarity_alone_cannot_separate_these_pairs():
    """**这条用例钉的是一个设计结论，不是一个函数。**

    2026-09-15 用 text-embedding-v4 实测 13 组真实问法对：同义 0.755–0.889，
    会答错的那批 0.596–0.908 —— 最危险的一对（商品总数 ⇄ 订单总数，0.9083）
    比所有同义对都高。没有任何一条 θ 能把两者分开。所以相似度只用来缩小候选，
    判据是内容词。下面每一对都取自那份实测。
    """
    same = [("商品总共有多少条记录", "商品一共有多少条"),
            ("商品总共有多少条记录", "统计一下商品的数量"),
            ("商品总共有多少条记录", "商品表里有多少行数据"),
            ("会员总数是多少", "一共有多少会员")]
    for a, b in same:
        assert semcache.lexically_same(a, b), (a, b)

    # 八条会答错的。**换了实体那一条的相似度是 0.9083 —— 全场最高。**
    danger = [("商品总共有多少条记录", "订单总共有多少条记录"),   # 换实体
              ("会员总数是多少", "商户总数是多少"),               # 换实体
              ("本月新增商品数", "上月新增商品数"),               # 换时间窗
              ("各品类商品数量分布", "各品类商品销售额分布"),     # 换聚合
              ("有多少商品在售", "有多少商品已下架"),             # 取反
              ("商品总共有多少条记录", "已下架的商品有多少条"),   # 加过滤
              ("商品总共有多少条记录", "今天新增了多少商品"),     # 加过滤
              ("商品总共有多少条记录", "评价平均分是多少")]       # 完全不同
    for a, b in danger:
        assert not semcache.lexically_same(a, b), (a, b)


def test_stop_words_never_swallow_a_discriminator():
    """功能词表只许收数量词/疑问词/套话。收进一个能区分问题的词，
    这道判据就会把两个不同的问题判成同一个 —— 而且不报错。"""
    for w in ("数量", "销售额", "新增", "在售", "下架", "本月", "上月", "商品", "订单"):
        assert w not in semcache._STOP_WORDS, w


def test_rejects_are_recorded_so_theta_can_be_calibrated():
    """够不着也要留痕：只记命中的话，"差一点就命中"那一片分布一个数都拿不到。"""
    semcache.reset()
    semcache._note_reject(0.86, "内容词不同")
    semcache._note_reject(0.83, "内容词不同")
    sh = semcache.stats()["shadow"]
    assert sh["reject:内容词不同"] == 2
    assert sh["top1:0.85"] == 1 and sh["top1:0.80"] == 1


# ------------------------------------------------ L2/L3 存储（连真库）
@pytest.fixture
def sem_store(_no_ambient_store, monkeypatch):
    """一个独立 schema 的语义缓存库。**不 skip** —— 与向量索引那组同一条口径：
    跳过等于这组一条都不跑，而报告还是绿的。"""
    import uuid as _uuid

    import psycopg

    from askdb import pgstore, vectors
    from tests.conftest import _test_store_dsn

    dsn = _test_store_dsn()
    if not dsn:
        pytest.fail("语义缓存用例需要一个可写的 PostgreSQL：设置 ASKDB_TEST_SOURCES_DSN")
    schema = f"askdb_sem_t_{_uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setenv(pgstore.DSN_ENV, dsn)
    monkeypatch.setenv(pgstore.SCHEMA_ENV, schema)
    pgstore.reset_pool()
    vectors.reset_cache()
    semcache.reset()
    try:
        yield schema
    finally:
        pgstore.reset_pool()
        vectors.reset_cache()
        semcache.reset()
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _payload(reasoning="共 1,234,567 行", sql="SELECT count(*) FROM skus", **kw):
    return {"ok": True, "rejected_by": None, "reasoning": reasoning,
            "caliber": "按 skus 全表计数", "sql_final": sql,
            "columns": ["n"], "rows": [[1234567]], "row_count": 1,
            "cost_cny": 0.0051, "elapsed_ms": 19840, **kw}


def test_remember_then_find_the_same_question(cfg, sem_store):
    v = [1.0, 0.0, 0.0]
    semcache.remember(cfg, "商品总共有多少条记录", v, _payload(),
                      org_id=0, role="DEV")
    got = semcache.lookup(cfg, "商品一共有多少条", v, org_id=0, role="DEV")
    assert got is not None and got.kind == "answer"
    assert got.question == "商品总共有多少条记录"
    assert semcache.stats()["stored"] == 1


def test_a_different_entity_is_rejected_even_at_similarity_1(cfg, sem_store):
    """**同一个向量**下换个实体也必须拦下 —— 相似度在这里已经没有话语权了，
    判据是内容词。这正是 0.9083 那一对的形状。"""
    v = [1.0, 0.0, 0.0]
    semcache.remember(cfg, "商品总共有多少条记录", v, _payload(),
                      org_id=0, role="DEV")
    assert semcache.lookup(cfg, "订单总共有多少条记录", v,
                           org_id=0, role="DEV") is None
    sh = semcache.stats()["shadow"]
    assert sh.get("reject:内容词不同") == 1
    assert any(k.startswith("top1:") for k in sh), "近邻分数要留痕，否则 θ 没法标定"


def test_another_role_or_org_never_sees_the_entry(cfg, sem_store):
    """scope 少一维就是跨身份串结果。"""
    v = [1.0, 0.0, 0.0]
    semcache.remember(cfg, "商品总共有多少条记录", v, _payload(),
                      org_id=0, role="DEV")
    assert semcache.lookup(cfg, "商品一共有多少条", v,
                           org_id=0, role="QA") is None
    assert semcache.lookup(cfg, "商品一共有多少条", v,
                           org_id=9, role="DEV") is None


def test_masking_change_invalidates_the_entry(cfg, sem_store):
    """脱敏列一改，scope 指纹跟着变，旧条目自动看不见 —— 与 L1 同一条口径。"""
    v = [1.0, 0.0, 0.0]
    semcache.remember(cfg, "商品总共有多少条记录", v, _payload(),
                      org_id=0, role="DEV")
    col = next(iter(next(iter(cfg.tables.values())).columns.values()))
    col.sensitive = not col.sensitive
    assert semcache.lookup(cfg, "商品一共有多少条", v,
                           org_id=0, role="DEV") is None


def test_time_sensitive_question_can_only_take_the_plan_lane(cfg, sem_store):
    v = [1.0, 0.0, 0.0]
    semcache.remember(cfg, "今天新增了多少商品", v,
                      _payload(reasoning="共 12 行", sql="SELECT count(*) FROM s"),
                      org_id=0, role="DEV")
    got = semcache.lookup(cfg, "今天新增了多少商品", v, org_id=0, role="DEV")
    assert got is not None and got.kind == "plan", "涉及时间的问题不许直答"
    assert got.why == "问题涉及时间"


def test_a_plan_with_a_literal_date_is_stored_but_never_reused(cfg, sem_store):
    v = [1.0, 0.0, 0.0]
    semcache.remember(
        cfg, "七月二十七日的商品数", v,
        _payload(reasoning="共 5 行",
                 sql="SELECT count(*) FROM s WHERE d = DATE '2026-07-27'"),
        org_id=0, role="DEV")
    # 直答那一路仍然走得通（它返回的是那天的旧结果，本来就是对的）；
    # 不可复用指的是**不许拿这条 SQL 去重跑**。
    got = semcache.lookup(cfg, "七月二十七日的商品数", v, org_id=0, role="DEV")
    assert got is None or got.kind == "answer"


def test_a_dirty_result_never_enters_the_store(cfg, sem_store):
    v = [1.0, 0.0, 0.0]
    semcache.remember(cfg, "商品总共有多少条记录", v,
                      _payload(truncated=True), org_id=0, role="DEV")
    assert semcache.stats()["stored"] == 0
    assert semcache.lookup(cfg, "商品一共有多少条", v,
                           org_id=0, role="DEV") is None


def test_lookup_without_a_vector_is_a_miss_not_an_error(cfg, sem_store):
    assert semcache.lookup(cfg, "商品一共有多少条", None,
                           org_id=0, role="DEV") is None
    semcache.remember(cfg, "q", None, _payload(), org_id=0, role="DEV")
    assert semcache.stats()["stored"] == 0


def test_the_extension_is_created_if_it_is_missing(cfg, sem_store):
    """**扩展要自己建。** schema 索引那条路只在真正触发一次向量召回时才建，
    而一台新部署完全可能先有人提问（走到这里）再有人触发索引 —— 少了这一步，
    这一层就永久降级，且只在 /api/health 里留一行字。
    """
    from askdb import pgstore
    assert semcache._ensure(cfg) is True
    assert pgstore.rows(
        "SELECT 1 FROM pg_extension WHERE extname = 'vector'"), "扩展没建起来"
    assert semcache.stats()["degraded"] == ""
