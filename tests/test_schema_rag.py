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
    assert schema_rag._score(t, "documents 有多少")[0] > 0
    assert schema_rag._score(t, "文档有多少")[0] > 0
    assert schema_rag._score(t, "完全无关的问题")[0] == 0


def test_generic_words_alone_do_not_count_as_a_hit(cfg):
    """"记录"这种泛词碰上 *_log / *_history 是必然，不是召回成功。

    实测：问"有多少条聊天记录"，靠"记录"二字召回了 security_audit_logs、
    user_password_history、tool_execution_log，真正该用的 agent_messages 一张
    没进 —— 而当时 blind=False，一句警告都不给。
    """
    from askdb.config import Column, Table

    cfg.raw["schema_rag"]["mode"] = "keyword"
    cfg.metrics = []
    cfg.tables = {
        n: Table(name=n, desc="", aliases=[],
                 columns={"id": Column("id", "BIGINT")}, tenant_exempt=True)
        for n in ("security_audit_logs", "tool_execution_log", "user_password_history",
                  "agent_messages", "agent_sessions", "users", "resumes", "job_matches")
    }
    cfg.raw["schema_rag"]["token_budget"] = 60      # 塞不下全库，只能走兜底那条
    cfg.raw["schema_rag"]["blind_budget"] = 60      # 盲选兜底预算也压死，测示警支
    r = schema_rag.recall("有多少条聊天记录", cfg)
    assert r.blind, "只靠泛词得分，必须仍按盲选处理并示警"


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
    """全都不过线时也要保底，不能让模型无表可用。

    2026-09-07 起"全都不过线"另外还是一次**盲选**：表少塞得下就整份给模型
    （见下一条），塞不下才退回保底 top_k 并示警。这里把预算压死，测的是
    保底那一支 —— 保底本身没有取消。
    """
    cfg.raw["schema_rag"]["mode"] = "vector"
    cfg.raw["schema_rag"]["top_k"] = 2
    cfg.raw["schema_rag"]["token_budget"] = 60
    cfg.raw["schema_rag"]["blind_budget"] = 60      # 盲选兜底也压死，测保底 top_k 那支
    idx = FakeIndex(("table:documents", 0.05), ("table:orgs", 0.04))
    r = schema_rag.recall("完全无关的问题", cfg, index=idx)
    assert len(r.tables) >= 1
    assert r.blind, "一条都没过线，保底选出来的表不能冒充召回成功"
    assert "相似度" in r.note


def test_vector_mode_blind_widens_to_all_tables_when_budget_allows(cfg):
    """向量模式下的盲选保护必须与关键词模式一致。

    换到 vector 之前这条判定只写在 keyword 那一支上（`if blind and
    mode == "keyword"`），照原样开 vector 等于把盲选示警整个关掉 ——
    开一个功能、关一层保护。
    """
    cfg.raw["schema_rag"]["mode"] = "vector"
    cfg.raw["schema_rag"]["token_budget"] = 5000
    idx = FakeIndex(("table:documents", 0.05), ("table:orgs", 0.04))
    r = schema_rag.recall("完全无关的问题", cfg, index=idx)
    assert set(r.table_names) == set(cfg.tables)
    assert not r.blind          # 全库都给了，不存在"看不见的表"
    assert "相似度" in r.note


def test_vector_fallback_note_survives_the_blind_note(cfg, monkeypatch):
    """"为什么回落"与"回落之后也没召到"是两条独立信息，不能互相覆盖。"""
    from askdb import vectors

    class Dead:
        def search(self, question, k):
            raise vectors.EmbeddingUnavailable("没配密钥")

    cfg.raw["schema_rag"]["mode"] = "vector"
    cfg.raw["schema_rag"]["token_budget"] = 60
    r = schema_rag.recall("完全无关的问题", cfg, index=Dead())
    assert "向量召回不可用" in r.note
    assert "关键词召回一张表都没命中" in r.note


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


# ---------------------------------------------------------------------------
# 中文提问的召回（2026-09-06 事故）
#
# 事故形状：_score 只做 `表名 in 问题` 的字面包含，中文问题对英文标识符恒为
# 0 分，全表并列，排序退化成白名单顺序 —— careermate 源 33 张表，5 条中文提问
# 召回到的永远是同样的前 3 张。不是拒答，是**答错还很笃定**：问"一共有多少个
# 用户"用 agent_messages 的 COUNT(DISTINCT user_id) 答了 6512，真值 10084。
# ---------------------------------------------------------------------------

def _wide_cfg(cfg, names):
    """一份"表多、注释全空"的白名单 —— 运行时数据源就长这样。"""
    from askdb.config import Column, Table

    # 本机配置走 mode: all，而这批用例锁的正是 keyword 那条路 —— 显式钉死，
    # 否则它们在"全库注入"下永远是绿的，什么也没验证。
    cfg.raw["schema_rag"]["mode"] = "keyword"
    cfg.tables = {
        n: Table(name=n, desc="", aliases=[],
                 columns={"id": Column("id", "BIGINT"),
                          "user_id": Column("user_id", "BIGINT"),
                          "created_at": Column("created_at", "TIMESTAMP")},
                 tenant_exempt=True)
        for n in names
    }
    cfg.metrics = []
    return cfg


CAREERMATE_LIKE = [
    "agent_messages", "interview_questions", "agent_tool_calls", "resume_versions",
    "security_audit_logs", "agent_task_states", "agent_sessions", "users",
    "user_profiles", "job_matches", "job_applications", "saved_jobs",
    "career_tasks", "study_notes", "interview_sessions", "resumes",
]


def test_chinese_question_reaches_the_right_table(cfg):
    """问"用户"就该召回 users —— 库里是英文，人问的是中文。"""
    c = _wide_cfg(cfg, CAREERMATE_LIKE)
    r = schema_rag.recall("一共有多少个用户", c)
    assert r.table_names[0] == "users"
    assert not r.blind


def test_chinese_question_is_not_answered_by_a_lookalike_table(cfg):
    """就是那条答错 6512 的问题：agent_messages 不能排在 users 前面。"""
    c = _wide_cfg(cfg, CAREERMATE_LIKE)
    r = schema_rag.recall("一共有多少个用户", c)
    assert r.table_names.index("users") < r.table_names.index("agent_messages") \
        if "agent_messages" in r.table_names else True


def test_recall_is_marked_blind_when_nothing_matches(cfg):
    """一张表都没命中时必须**说出来**，而不是把兜底当成召回结果往下跑。"""
    c = _wide_cfg(cfg, CAREERMATE_LIKE)
    c.raw["schema_rag"]["token_budget"] = 200          # 塞不下全库，只能兜底
    c.raw["schema_rag"]["blind_budget"] = 200          # 盲选兜底预算也压死，测示警支
    r = schema_rag.recall("今天天气怎么样", c)
    assert r.blind
    assert r.note


def test_blind_recall_widens_to_all_tables_when_budget_allows(cfg):
    """表少到能全给时，全给 —— 模型看得见全部表名就不会挑错。"""
    c = _wide_cfg(cfg, ["users", "orders"])
    c.raw["schema_rag"]["token_budget"] = 5000
    r = schema_rag.recall("今天天气怎么样", c)
    assert set(r.table_names) == {"users", "orders"}
    assert not r.blind          # 全库都给了，不存在"看不见的表"
    assert r.note


def test_table_comment_beats_the_builtin_dictionary(cfg):
    """库里的中文表注释是最准的一份语义，权重要压过词典。"""
    from askdb.config import Column, Table

    cfg.raw["schema_rag"]["mode"] = "keyword"
    cfg.metrics = []
    cfg.tables = {
        "t_zzz": Table(name="t_zzz", desc="岗位投递记录", aliases=[],
                       columns={"id": Column("id", "BIGINT")}, tenant_exempt=True),
        "users": Table(name="users", desc="", aliases=[],
                       columns={"id": Column("id", "BIGINT")}, tenant_exempt=True),
    }
    cfg.raw["schema_rag"]["top_k"] = 1
    cfg.raw["schema_rag"]["max_k"] = 1
    r = schema_rag.recall("岗位投递有多少条", cfg)
    assert r.table_names[0] == "t_zzz"


# ------------------------------------------------- 2026-09-09 十二源回归修复
#
# 这一组测的都是同一种失败：表明明在白名单里，模型却回答"库里没有这类数据"。
# 它比拒答危险 —— 用户会相信"确实没有"。

def test_alias_written_in_the_comment_is_usable(cfg):
    """运行时数据源的别名写在库注释里（"别名：异常单、问题订单"），
    扫描时并不会落进 Table.aliases。不解析出来，这份最准的语义就白存了。"""
    from askdb.config import Table
    t = Table(name="order_exceptions", aliases=[],
              desc="异常订单标记。需要人工介入。别名：异常单、问题订单", columns={})
    assert schema_rag.alias_hints(t) == ["异常单", "问题订单"]


def test_alias_hints_keeps_declared_aliases(cfg):
    from askdb.config import Table
    t = Table(name="t", aliases=["显式"], desc="说明。别名：甲、乙", columns={})
    assert schema_rag.alias_hints(t) == ["显式", "甲", "乙"]


def test_bigram_overlap_reaches_a_table_whose_comment_only_partly_matches(cfg):
    """注释写「异常订单标记」，提问问「异常订单」—— 整词匹配是 0 分。
    2-gram 重合 3 个，这正是线上把 order_exceptions 漏掉的那一步。"""
    assert len(schema_rag._bigrams("异常订单标记") & schema_rag._bigrams("未解决的异常订单")) >= 3


def test_summary_tables_are_found_by_name_and_comment(cfg):
    from askdb.config import Table
    cfg.tables = {
        "orders": Table(name="orders", aliases=[], desc="订单主表。一行一笔订单", columns={}),
        "order_daily_stats": Table(name="order_daily_stats", aliases=[], desc="订单按日汇总", columns={}),
        # 注释里带"汇总到 orders"的明细表**不能**被认成汇总表，
        # 误判进来等于把最该避开的大表当成了捷径
        "order_items": Table(name="order_items", aliases=[], desc="订单明细。行金额汇总到 orders", columns={}),
    }
    names = [t.name for t in schema_rag.summary_tables(cfg)]
    assert names == ["order_daily_stats"]


def test_summary_hint_is_empty_when_the_source_has_none(cfg):
    from askdb.config import Table
    cfg.tables = {"orders": Table(name="orders", aliases=[], desc="订单主表", columns={})}
    assert schema_rag.summary_hint(cfg) == ""


# ------------------------------------------------- 召回后端的降级必须体检得出来

def test_backend_status_reports_declared_mode(cfg):
    from askdb import schema_rag

    cfg.raw["schema_rag"]["mode"] = "keyword"
    st = schema_rag.backend_status(cfg)
    assert st["mode"] == "keyword" and st["effective"] == "keyword"
    assert not st["degraded"]


def test_backend_status_flags_missing_vector_backend(cfg, monkeypatch):
    """声明 vector、装不上 chromadb —— 生产上就这么静默回落了很久。

    健康检查必须说得出这件事：它决定了模型到底看得见哪几张表。
    """
    import builtins

    from askdb import schema_rag

    cfg.raw["schema_rag"]["mode"] = "vector"
    real = builtins.__import__

    def no_chroma(name, *a, **k):
        if name == "chromadb":
            raise ImportError("No module named 'chromadb'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_chroma)
    st = schema_rag.backend_status(cfg)
    assert st["degraded"] and st["effective"] == "keyword"
    assert "chromadb" in st["reason"]
