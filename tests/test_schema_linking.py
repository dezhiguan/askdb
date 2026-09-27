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
def test_candidates_strip_stopwords(cfg):
    """虚词划掉，剩下的才是值。"张三"必须抽得出来。"""
    got = valuelink.candidates("查一下张三的会话数", cfg)
    assert any("张三" in g for g in got)


def test_metadata_words_are_demoted_not_dropped(cfg):
    """元数据词**降权而不排除**。

    排除过一版，生产上栽了：customer_tags 的注释举例写着"如高价值、流失
    预警"，于是"高价值客户"整体被判成元数据词、一个候选都抽不出来 ——
    而它正是库里的一行数据。两种错的代价不对等，见 _segments 的说明。
    """
    got = valuelink.candidates("本月文档总数是多少", cfg)
    # 抽得出来（允许探一次），但必须排在后面
    if got:
        assert valuelink._metaish(got[-1], valuelink._schema_words(cfg))


def test_value_like_phrase_survives_example_in_comment(cfg):
    """注释里举过例的词，不能因此就不算值了。"""
    known = valuelink._schema_words(cfg)
    segs = dict(valuelink._segments("高价值客户", known))
    assert "高价值客户" in segs


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


# --------------------------------------------------------------------------
# 五、列锚点
# 生产 trace 67fc0bcf4c6a：问邮箱和手机号，召回 14 张表。
# --------------------------------------------------------------------------
def _anchor_tables():
    users = _t("users", {
        "id": "BIGINT", "email": "VARCHAR", "phone": "VARCHAR",
        "phone_verified": "BOOLEAN", "username": "VARCHAR",
    })
    runs = _t("resume_generation_run", {"id": "BIGINT", "user_id": "BIGINT"})
    actions = _t("agent_pending_actions", {"id": "BIGINT", "user_id": "BIGINT"})
    contacts = _t("user_contacts", {
        "id": "BIGINT", "email": "VARCHAR", "phone": "VARCHAR",
    })
    fillers = [_t(f"t{i}", {"id": "BIGINT", "user_id": "BIGINT"}) for i in range(8)]
    tabs = _tables(users, runs, actions, contacts, *fillers)
    infer_foreign_keys(tabs)
    return tabs


class _AnchorCfg:
    # _render 走 L0，key 里要这两项；没有默认源时方言取不到，scope 自己会兜。
    source_id = ""
    path = ""

    def __init__(self, tables, **rag):
        raw = {
            "mode": "keyword", "top_k": 3, "max_k": 12,
            "token_budget": 8000, "fk_expand_max": 3,
            "value_link": False, "coverage_check": "off",
            "attr_anchor": True,
        }
        raw.update(rag)
        self.tables = tables
        self.metrics = []
        self.raw = {"schema_rag": raw}


def test_phone_number_does_not_also_match_phone():
    """「手机号」吃掉之后不能再记一次「手机」。"""
    got = [cn for cn, _ in schema_rag.attribute_mentions("用户的邮箱和手机号分别是什么")]
    assert got == ["邮箱", "手机号"]


def test_verified_flag_is_not_the_phone_column():
    """phone_verified 不是手机号。只有它、没有 phone 列时，锚点不成立。"""
    tabs = _tables(_t("users", {"id": "BIGINT", "phone_verified": "BOOLEAN"}))
    assert schema_rag.attribute_anchor("用户的手机号是什么", _AnchorCfg(tabs)) == ([], [])


def test_attribute_anchor_narrows_contact_lookup():
    """邮箱和手机号都在 users 上，且只有这一张表同时有这两列。

    外键邻居（resume_generation_run、agent_pending_actions）不该再被补进来。
    """
    tabs = _anchor_tables()
    # user_contacts 只留 email，避免和 users 并列成两张都能答的表
    tabs["user_contacts"] = _t("user_contacts", {"id": "BIGINT", "email": "VARCHAR"})
    infer_foreign_keys(tabs)
    q = "用户的邮箱和手机号分别是什么"
    wide = schema_rag.recall(q, _AnchorCfg(tabs, attr_anchor=False))
    narrow = schema_rag.recall(q, _AnchorCfg(tabs))
    assert "users" in wide.table_names and len(wide.table_names) > 1
    assert narrow.table_names == ["users"]
    assert narrow.fk_added == []
    assert narrow.attr_anchor == ["users"]
    assert narrow.attr_labels == ["邮箱", "手机号"]
    assert "resume_generation_run" not in narrow.table_names
    assert "agent_pending_actions" not in narrow.table_names


def test_two_tables_covering_the_same_attributes_are_not_narrowed():
    """users 和 user_contacts 都能答，分不出主表，保持宽召回。"""
    tabs = _anchor_tables()
    assert schema_rag.attribute_anchor(
        "用户的邮箱和手机号分别是什么", _AnchorCfg(tabs)) == ([], [])


def test_second_entity_blocks_the_anchor():
    """「订单」不在 users 上，不能因为邮箱对上了就把订单表丢掉。"""
    tabs = _anchor_tables()
    tabs["orders"] = _t("orders", {"id": "BIGINT", "user_id": "BIGINT"})
    assert schema_rag.attribute_anchor(
        "用户的邮箱和订单数分别是多少", _AnchorCfg(tabs)) == ([], [])


def test_grouped_question_is_not_anchored():
    tabs = _anchor_tables()
    assert schema_rag.attribute_anchor(
        "每个用户的邮箱分别是什么", _AnchorCfg(tabs)) == ([], [])
