"""召回测试：命中、口径连带、token 预算与裁剪告警。"""

from __future__ import annotations

from askdb import schema_rag


def test_recall_hits_relevant_table(cfg):
    r = schema_rag.recall("有哪些文档卡在处理中", cfg)
    assert "documents" in r.table_names


def test_recall_hits_metric_by_alias(cfg):
    r = schema_rag.recall("有哪些文档卡在处理中超过一小时", cfg)
    assert "卡住的文档" in [m.name for m in r.metrics]


def test_metric_scope_tables_are_force_injected(cfg):
    """命中口径涉及的表必须一并注入，否则口径表达式引用的列不可见。"""
    cfg.raw["schema_rag"]["top_k"] = 1
    cfg.raw["schema_rag"]["max_k"] = 1
    r = schema_rag.recall("这个月的成本 卡住的文档", cfg)
    assert "documents" in r.table_names


def test_all_mode_injects_everything(cfg):
    cfg.raw["schema_rag"]["mode"] = "all"
    r = schema_rag.recall("随便问点什么", cfg)
    assert set(r.table_names) == set(cfg.tables)
    assert r.mode == "all"


def test_recall_never_returns_empty(cfg):
    """一个词都没命中时也要补齐，否则模型无表可用。"""
    r = schema_rag.recall("xyzzy", cfg)
    assert len(r.tables) >= 1


def test_prompt_marks_tenant_column(cfg):
    r = schema_rag.recall("文档", cfg)
    assert "租户隔离列" in r.prompt and "不要自己写" in r.prompt


def test_prompt_contains_enum_values(cfg):
    r = schema_rag.recall("文档状态", cfg)
    assert "PROCESSING" in r.prompt


def test_metric_doc_forbids_self_construction(cfg):
    r = schema_rag.recall("卡住的文档", cfg)
    assert "不得自行构造" in r.prompt


def test_token_budget_truncates_and_reports(cfg):
    """超预算必须记录被裁掉的表 —— 静默截断会造成不可解释的准确率下降。"""
    cfg.raw["schema_rag"]["mode"] = "all"
    cfg.raw["schema_rag"]["token_budget"] = 60
    r = schema_rag.recall("文档 知识库 组织 成本", cfg)
    assert r.truncated
    assert len(r.tables) >= 1


def test_est_tokens_counts_cjk_per_char():
    assert schema_rag._est_tokens("中文中文") == 4
    assert schema_rag._est_tokens("abcd") == 1


def test_score_prefers_name_and_alias(cfg):
    t = cfg.tables["documents"]
    assert schema_rag._score(t, "documents 有多少") > 0
    assert schema_rag._score(t, "文档有多少") > 0
    assert schema_rag._score(t, "完全无关的问题") == 0


def test_table_doc_renders_columns(cfg):
    doc = schema_rag.table_doc(cfg.tables["documents"])
    assert "documents" in doc and "status" in doc


# ---------------------------------------------------------------- 向量召回

class FakeIndex:
    """按预置顺序返回命中，不打网络。"""

    def __init__(self, *pairs):        # (key, score)
        self.pairs = list(pairs)
        self.asked: list[int] = []

    def search(self, question, k):
        from askdb.vectors import Hit
        self.asked.append(k)
        return [Hit(key=key, score=score) for key, score in self.pairs][:k]


def test_vector_mode_uses_index_ranking(cfg):
    cfg.raw["schema_rag"]["mode"] = "vector"
    cfg.raw["schema_rag"]["top_k"] = 2          # 否则保底逻辑会把第 3 张也补进来
    idx = FakeIndex(("table:model_usage", 0.81), ("table:documents", 0.42), ("table:orgs", 0.10))
    r = schema_rag.recall("这个月烧了多少钱", cfg, index=idx)
    assert r.mode == "vector"
    assert r.table_names[0] == "model_usage"
    assert "orgs" not in r.table_names          # 0.10 低于阈值，视为干扰


def test_vector_mode_keeps_top_k_even_below_threshold(cfg):
    """全都不过线时也要保底，不能让模型无表可用。"""
    cfg.raw["schema_rag"]["mode"] = "vector"
    cfg.raw["schema_rag"]["top_k"] = 2
    idx = FakeIndex(("table:documents", 0.05), ("table:orgs", 0.04))
    r = schema_rag.recall("完全无关的问题", cfg, index=idx)
    assert len(r.tables) == 2


def test_vector_mode_recalls_metrics_by_semantics(cfg):
    """别名没写全时，靠语义把口径捞回来。"""
    cfg.raw["schema_rag"]["mode"] = "vector"
    idx = FakeIndex(("table:documents", 0.7), ("metric:卡住的文档", 0.55))
    r = schema_rag.recall("哪些资料一直没解析完", cfg, index=idx)
    assert "卡住的文档" in [m.name for m in r.metrics]


def test_vector_mode_caps_metric_count(cfg):
    """口径是强约束，塞多了会误导模型。"""
    cfg.raw["schema_rag"]["mode"] = "vector"
    cfg.raw["schema_rag"]["max_metrics"] = 1
    idx = FakeIndex(("table:documents", 0.7),
                    ("metric:卡住的文档", 0.6), ("metric:文档数", 0.55), ("metric:失败率", 0.5))
    r = schema_rag.recall("随便问问", cfg, index=idx)
    assert len(r.metrics) <= 1


def test_vector_mode_ignores_low_score_metrics(cfg):
    cfg.raw["schema_rag"]["mode"] = "vector"
    idx = FakeIndex(("table:documents", 0.7), ("metric:文档数", 0.05))
    r = schema_rag.recall("随便问问", cfg, index=idx)
    assert r.metrics == []


def test_vector_failure_falls_back_to_keyword(cfg):
    """召回退化只是准确率下降，不该让整条链路不可用。"""
    from askdb.vectors import EmbeddingUnavailable

    class Broken:
        def search(self, q, k):
            raise EmbeddingUnavailable("没有密钥")

    cfg.raw["schema_rag"]["mode"] = "vector"
    r = schema_rag.recall("有哪些文档", cfg, index=Broken())
    assert r.mode == "keyword" and r.tables
    assert "回落" in r.note


def test_vector_requests_more_than_max_k(cfg):
    """表和口径同在一个索引里，只取 max_k 会互相挤占名额。"""
    cfg.raw["schema_rag"]["mode"] = "vector"
    idx = FakeIndex(("table:documents", 0.7))
    schema_rag.recall("q", cfg, index=idx)
    assert idx.asked[0] > cfg.raw["schema_rag"]["max_k"]


def test_fingerprint_changes_with_schema(cfg):
    """schema 变了索引必须重建，否则召回的是旧结构 —— 这种错极难定位。"""
    from askdb.vectors import _fingerprint

    before = _fingerprint(cfg)
    cfg.tables["documents"].desc += "（改过）"
    assert _fingerprint(cfg) != before


def test_grain_enters_the_prompt_as_a_hard_constraint(cfg):
    """粒度必须进提示词，而且语气要比「说明」更重。

    expr 是**片段注入**：口径保证了表达式本身，保证不了它被放进什么查询里。
    「日均成本」= SUM(cost)/COUNT(DISTINCT stat_date)，模型若再 GROUP BY model，
    分母就从"全期天数"变成"该模型有记录的天数" —— 两个数都合法、都跑得出来、
    护栏一条都不会触发。粒度只写在 note 里等于指望模型自己读懂。
    """
    from askdb.config import Metric
    from askdb.schema_rag import metric_doc

    m = Metric(name="日均成本", aliases=["日均花费"], scope=["model_usage_daily"],
               expr="SUM(cost) / NULLIF(COUNT(DISTINCT stat_date), 0)",
               grain="全期一个数，不得再按模型或用途分组",
               note="分母是有记录的天数")
    doc = metric_doc(m)
    assert "不得再按模型或用途分组" in doc
    assert "聚合粒度" in doc
    # 没写粒度的口径不该凭空多出一行
    assert "聚合粒度" not in metric_doc(Metric(name="x", aliases=[], scope=[], expr="COUNT(*)"))
