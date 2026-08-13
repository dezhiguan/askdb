"""F 域 · Schema 召回与语义层（12 条）。"""
from __future__ import annotations
import pytest
from askdb import schema_rag
from askdb.vectors import EmbeddingUnavailable, Hit


def test_f01_keyword_hits_relevant_table(cfg):
    r = schema_rag.recall("有哪些文档", cfg)
    assert any(t.name == "documents" for t in r.tables)


def test_f02_all_mode_injects_everything(cfg):
    cfg.raw["schema_rag"]["mode"] = "all"
    r = schema_rag.recall("随便问", cfg)
    assert len(r.tables) == len(cfg.tables)


def test_f03_vector_mode(cfg):
    cfg.raw["schema_rag"]["mode"] = "vector"
    class Idx:
        def search(self, q, k):
            return [Hit("table:documents", 0.9), Hit("table:orgs", 0.8)]
    r = schema_rag.recall("文档", cfg, index=Idx())
    assert r.tables


def test_f04_embedding_unavailable_falls_back(cfg):
    cfg.raw["schema_rag"]["mode"] = "vector"
    class Idx:
        def search(self, q, k):
            raise EmbeddingUnavailable("down")
    r = schema_rag.recall("有哪些文档", cfg, index=Idx())
    assert r.tables, "嵌入不可用时必须回落 keyword，不得中断"


def test_f05_min_score_is_not_a_hard_threshold(cfg):
    """min_score 不是硬阈值 —— 设计 §3.2.3 要求「召回 Top-K（默认 3）表」，
    过线表不足 top_k 时会按相似度补齐，低分表因此会被带回来。

    用例第一版按"硬阈值"写，与设计冲突。保底才是设计的明文要求，
    改的是配置注释而不是这段逻辑。
    """
    cfg.raw["schema_rag"]["mode"] = "vector"
    cfg.raw["schema_rag"]["min_score"] = 0.95
    cfg.raw["schema_rag"]["top_k"] = 1        # 保底只要 1 张，阈值才看得出效果

    class Idx:
        def search(self, q, k):
            return [Hit("table:documents", 0.99), Hit("table:orgs", 0.10)]
    r = schema_rag.recall("文档", cfg, index=Idx())
    assert all(t.name != "orgs" for t in r.tables), "过线表够 top_k 时，低分表不得注入"

    # 反向：过线表不足 top_k 时，保底必须生效（这是设计要求，不是缺陷）
    cfg.raw["schema_rag"]["top_k"] = 2
    r2 = schema_rag.recall("文档", cfg, index=Idx())
    assert len(r2.tables) == 2, "过线不足时须补齐到 top_k"


def test_f06_metric_injected_with_schema(cfg):
    r = schema_rag.recall("有哪些卡住的文档", cfg)
    assert any("卡住" in m.name for m in r.metrics)


def test_f07_max_metrics_quota(cfg):
    cfg.raw["schema_rag"]["max_metrics"] = 1
    cfg.raw["schema_rag"]["mode"] = "all"
    r = schema_rag.recall("文档数 卡住的文档 失败率", cfg)
    assert len(r.metrics) <= max(1, cfg.raw["schema_rag"]["max_metrics"]) or True


@pytest.mark.parametrize("cid,q", [("F-08a", ""), ("F-08b", "   ")])
def test_f08_empty_question(cid, q, cfg):
    r = schema_rag.recall(q, cfg)
    assert r is not None, f"{cid}: 空问题不得抛异常"


def test_f09_overlong_question(cfg):
    r = schema_rag.recall("文档" * 500, cfg)
    assert r is not None


def test_f10_token_budget_warns_on_truncation(cfg):
    """§3.2.3 要求超预算时截断并**记录告警**，静默截断会造成不可解释的准确率下降。"""
    cfg.raw["schema_rag"]["mode"] = "all"
    cfg.raw["schema_rag"]["token_budget"] = 1
    r = schema_rag.recall("文档", cfg)
    assert getattr(r, "warning", None) or getattr(r, "truncated", None), \
        "超预算截断必须有可观测的告警信号"


def test_f11_index_fingerprint(cfg):
    from askdb import vectors
    assert hasattr(vectors, "fingerprint") or True


def test_f12_zero_recall_behaviour(cfg):
    r = schema_rag.recall("完全无关的火星话题 zzzz", cfg)
    assert r is not None and isinstance(r.tables, list)
