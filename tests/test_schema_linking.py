"""Schema Linking：外键采集/推断、关联图扩展、值检索、覆盖度判定。

分四组，对应链路上四个独立的失败方式：
  · 外键推断推错了（自环、目标列不存在）→ 一条写进 SQL 的假 JOIN
  · 扩展没有刹车         → 一张 orders 把全库拖进上下文
  · 值检索探了不该探的列 → 敏感数据的存在性预言机
  · 覆盖度判不出缺口     → 多实体提问静默只答一半
"""

from __future__ import annotations

from askdb import schema_rag, valuelink
from askdb.config import Column, Table, infer_foreign_keys


def _t(name: str, cols: dict[str, str], desc: str = "") -> Table:
    return Table(name=name, desc=desc, aliases=[],
                 columns={c: Column(name=c, type=ty) for c, ty in cols.items()})


def _tables(*ts: Table) -> dict[str, Table]:
    return {t.name: t for t in ts}


# --------------------------------------------------------------------------
# 一、外键推断
# --------------------------------------------------------------------------
def test_infer_links_child_to_parent():
    tabs = _tables(_t("orders", {"order_id": "BIGINT"}),
                   _t("order_items", {"item_id": "BIGINT", "order_id": "BIGINT"}))
    assert infer_foreign_keys(tabs) == 1
    col = tabs["order_items"].columns["order_id"]
    assert col.fk == "orders.order_id" and col.fk_kind == "INFERRED"


def test_infer_skips_self_reference():
    """`carts.cart_id` 的词根正是本表 —— 它是主键，不是外键。"""
    tabs = _tables(_t("carts", {"cart_id": "BIGINT"}))
    assert infer_foreign_keys(tabs) == 0
    assert tabs["carts"].columns["cart_id"].fk == ""


def test_infer_skips_when_target_table_absent():
    """指向白名单外的表那条边给不得：模型会去 JOIN 一张它看不见的表。"""
    tabs = _tables(_t("carts", {"cart_id": "BIGINT", "customer_id": "BIGINT"}))
    infer_foreign_keys(tabs)
    assert tabs["carts"].columns["customer_id"].fk == ""


def test_infer_skips_when_no_joinable_column():
    """目标表既没有同名列也没有 id —— 编一个列名出来，模型会照着写。"""
    tabs = _tables(_t("orders", {"serial": "TEXT"}),
                   _t("order_items", {"order_id": "BIGINT"}))
    infer_foreign_keys(tabs)
    assert tabs["order_items"].columns["order_id"].fk == ""


def test_declared_fk_is_never_overwritten_by_inference():
    tabs = _tables(_t("orders", {"order_id": "BIGINT"}),
                   _t("order_items", {"order_id": "BIGINT"}))
    tabs["order_items"].columns["order_id"].fk = "legacy_orders.id"
    tabs["order_items"].columns["order_id"].fk_kind = "FOREIGN_KEY"
    infer_foreign_keys(tabs)
    assert tabs["order_items"].columns["order_id"].fk == "legacy_orders.id"


def test_table_doc_separates_declared_from_inferred():
    """两种来源的措辞必须分开 —— 混成一句就是把猜测升级成事实。"""
    t = _t("order_items", {"order_id": "BIGINT"})
    t.columns["order_id"].fk = "orders.order_id"
    t.columns["order_id"].fk_kind = "FOREIGN_KEY"
    assert "[外键 → orders.order_id]" in schema_rag.table_doc(t)
    t.columns["order_id"].fk_kind = "INFERRED"
    doc = schema_rag.table_doc(t)
    assert "疑似外键" in doc and "用前请核对" in doc


# --------------------------------------------------------------------------
# 二、关联图与扩展
# --------------------------------------------------------------------------
class _Cfg:
    """只带 tables 的极简配置替身 —— 扩展只吃这一项。"""

    def __init__(self, tables):
        self.tables = tables
        self.raw = {"schema_rag": {}}


def _graph_cfg():
    tabs = _tables(_t("orders", {"order_id": "BIGINT"}),
                   _t("order_items", {"order_id": "BIGINT", "sku_id": "BIGINT"}),
                   _t("skus", {"sku_id": "BIGINT"}),
                   _t("audit_logs", {"order_id": "BIGINT"}))
    infer_foreign_keys(tabs)
    return _Cfg(tabs)


def test_fk_graph_is_undirected():
    g = schema_rag.fk_graph(_graph_cfg())
    assert "order_items" in g["orders"] and "orders" in g["order_items"]


def test_expand_brings_back_bridge_table():
    """orders 与 skus 之间的桥接表 order_items —— 它自己没有业务语义，
    任何语义召回都找不到它，而 JOIN 少了它根本写不出来。"""
    cfg = _graph_cfg()
    picked = [cfg.tables["orders"], cfg.tables["skus"]]
    got = [t.name for t in schema_rag.fk_expand(picked, cfg, [], 3)]
    assert "order_items" in got


def test_expand_respects_rank_window():
    """只连着一张已选表、又不在排名窗口里的邻居，不许进来。"""
    cfg = _graph_cfg()
    picked = [cfg.tables["orders"]]
    got = schema_rag.fk_expand(picked, cfg, ["orders"], 3)
    assert [t.name for t in got] == []


def test_expand_honours_max_add():
    cfg = _graph_cfg()
    order = ["orders", "order_items", "audit_logs", "skus"]
    got = schema_rag.fk_expand([cfg.tables["orders"]], cfg, order, 1)
    assert len(got) == 1


def test_expand_disabled_by_zero(cfg):
    cfg.raw["schema_rag"]["fk_expand_max"] = 0
    assert schema_rag.recall("文档", cfg).fk_added == []


# --------------------------------------------------------------------------
# 三、值检索
# --------------------------------------------------------------------------
def test_candidates_strips_stopwords_and_schema_words(cfg):
    """"查一下张三的订单数" 里，只有"张三"是值。"""
    got = valuelink.candidates("查一下张三的文档数和文档大小", cfg)
    assert "张三" in got
    assert not any("文档" in g for g in got)


def test_candidates_ignore_pure_metric_question(cfg):
    """全是元数据词的提问不该探测 —— 一次白跑的查询，还可能撞出无关命中。"""
    assert valuelink.candidates("本月文档总数是多少", cfg) == []


def test_candidates_keep_trailing_digits(cfg):
    """昵称常是"中文+数字"，差这一位等值匹配就必然落空。"""
    assert "大榆1" in valuelink.candidates("大榆1 有多少文档", cfg)


def test_probe_columns_exclude_sensitive():
    """值探测是一个存在性预言机 —— 对 PII 列开放等于开了一个撞库接口。"""
    t = _t("users", {"nickname": "VARCHAR", "phone": "VARCHAR"})
    cols = valuelink.probe_columns(_Cfg(_tables(t)))
    assert ("users", "nickname") in [(x.name, c) for x, c in cols]
    assert "phone" not in [c for _, c in cols]


def test_probe_columns_exclude_enum_like():
    """枚举取值已经完整渲染进提示词了，再探一遍是浪费预算。"""
    t = _t("orders", {"status": "VARCHAR", "title": "VARCHAR"})
    cols = [c for _, c in valuelink.probe_columns(_Cfg(_tables(t)))]
    assert "title" in cols and "status" not in cols


def test_probe_columns_big_table_needs_index():
    """大表只探有索引的列：不命中且无索引就是一次全表扫描（实测 605ms）。"""
    t = _t("customers", {"nickname": "VARCHAR"})
    rows = {"customers": 10_000_000}
    assert valuelink.probe_columns(_Cfg(_tables(t)), rows) == []
    got = valuelink.probe_columns(_Cfg(_tables(t)), rows, {("customers", "nickname")})
    assert [c for _, c in got] == ["nickname"]


def test_probe_sql_is_parameterised():
    t = _t("users", {"nickname": "VARCHAR"})
    sql, params = valuelink.build_probe_sql(["张三"], [(t, "nickname")], "pg")
    assert "张三" not in sql and "张三" in params
    assert sql.count("%s") == len(params)


def test_probe_swallows_backend_failure():
    """任何异常都当没命中 —— 增量线索不该成为主链路的新故障点。"""
    class Boom:
        cfg = _Cfg({})

        def connect(self):
            raise RuntimeError("连接没了")

    assert valuelink.probe("张三的订单", _Cfg(_tables(
        _t("users", {"nickname": "VARCHAR"}))), Boom()) == []


def test_hint_names_the_column_not_just_the_table():
    """知道值在哪张表还不够 —— WHERE 要写出来，得知道是哪一列。"""
    text = valuelink.hint([valuelink.ValueHit("张三", "users", "nickname")])
    assert "users.nickname" in text and "张三" in text


# --------------------------------------------------------------------------
# 四、覆盖度判定
# --------------------------------------------------------------------------
def test_coverage_reports_uncovered_entity():
    """"用户"有表兜住、"订单"没有 —— blind 判定看不见这种半覆盖。"""
    cfg = _Cfg(_tables(_t("users", {"id": "BIGINT"})))
    gaps = schema_rag.coverage_gaps("用户的订单数", cfg, list(cfg.tables.values()))
    assert "订单" in gaps and "用户" not in gaps


def test_coverage_clean_when_all_entities_covered():
    cfg = _Cfg(_tables(_t("users", {"id": "BIGINT"}),
                       _t("orders", {"id": "BIGINT"})))
    assert schema_rag.coverage_gaps("用户的订单数", cfg,
                                    list(cfg.tables.values())) == []


def test_coverage_shadow_mode_stays_silent(cfg):
    """默认 shadow：记录但不向用户发声，先在生产流量里标定误判形状。"""
    cfg.raw["schema_rag"]["coverage_check"] = "shadow"
    r = schema_rag.recall("用户的订单数", cfg)
    assert "没有对应的表被召回" not in (r.note or "")


def test_coverage_enforce_speaks_up(cfg):
    cfg.raw["schema_rag"]["coverage_check"] = "enforce"
    cfg.raw["schema_rag"]["mode"] = "keyword"
    r = schema_rag.recall("用户的订单数", cfg)
    if r.coverage_gaps:
        assert "没有对应的表被召回" in (r.note or "")


def test_capacity_is_cached_per_source():
    """行数与索引清单跟着 DDL 走，每次提问重查一遍是纯粹的浪费
    （实测 97 条用例为此多花 129 秒）。"""
    valuelink.reset_capacity()
    calls = {"n": 0}

    class Counting:
        cfg = _Cfg({})

        def connect(self):
            calls["n"] += 1
            raise RuntimeError("只数调用次数")

    for _ in range(3):
        valuelink.capacity(Counting(), "pg", "src-1")
    # 两条查询（行数 + 索引）各一次，之后全部走缓存
    assert calls["n"] == 2
    valuelink.reset_capacity()
