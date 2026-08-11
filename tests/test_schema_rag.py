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
