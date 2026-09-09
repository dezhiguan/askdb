"""Schema 向量索引（PostgreSQL + pgvector）。

用例钉的是**换掉 Chroma 那几个理由**能不能立住：索引要真的落在库里（多副本
共享、重启不丢）、指纹变了要换一份并把旧的收掉、以及每一条"走不通"的路
都必须折成 EmbeddingUnavailable 让召回回落 —— 而不是把整条问答链路带塌。

嵌入一律用假的：真调 DashScope 会让这组用例变成一次网络与账单的赌博，
而这里要验的是存储与降级，不是模型。
"""

from __future__ import annotations

import uuid

import pytest

from askdb import pgstore, vectors
from tests.conftest import _test_store_dsn


@pytest.fixture
def vec_store(_no_ambient_store, monkeypatch):
    """一个独立 schema 的向量库。

    **不 skip。** 与凭据库那组同一条口径：跳过等于这组用例一条都不跑，
    而报告还是绿的 —— 向量召回自 2026-09-09 起只有 PG 一种存储。
    """
    import psycopg

    dsn = _test_store_dsn()
    if not dsn:
        pytest.fail("向量索引用例需要一个可写的 PostgreSQL：设置 ASKDB_TEST_SOURCES_DSN")
    schema = f"askdb_vec_t_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setenv(pgstore.DSN_ENV, dsn)
    monkeypatch.setenv(pgstore.SCHEMA_ENV, schema)
    pgstore.reset_pool()
    vectors.reset_cache()
    try:
        yield schema
    finally:
        pgstore.reset_pool()
        vectors.reset_cache()
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


class FakeEmbedder:
    """确定性假嵌入：按关键词给三维向量，方向差异即语义差异。

    不做归一化 —— pgvector 的 `<=>` 本来就是余弦距离，长度不参与，
    这里若归一化反而会掩盖"传进去的是不是同一个向量"这件事。
    """

    def __init__(self):
        self.docs_calls = 0

    def _vec(self, text: str) -> list[float]:
        return [
            3.0 if "文档" in text else 0.1,
            3.0 if "组织" in text or "orgs" in text else 0.1,
            3.0 if "成本" in text or "usage" in text else 0.1,
        ]

    def embed_documents(self, docs):
        self.docs_calls += 1
        return [self._vec(d) for d in docs]

    def embed_query(self, q):
        return self._vec(q)


@pytest.fixture
def fake_embed(monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(vectors.VectorIndex, "_embedder", lambda self: emb)
    return emb


# ------------------------------------------------------------------ 建索引与检索

def test_search_builds_index_on_first_use_and_ranks_by_similarity(
        cfg, vec_store, fake_embed):
    idx = vectors.VectorIndex(cfg)
    hits = idx.search("哪些文档卡住了", 8)

    assert hits, "首次使用应自动建索引，而不是空手而归"
    # 表与口径在同一个索引里（recall 靠 key 前缀各取各的），所以这里不钉
    # "第一条必须是表"，只钉最相近的那张表是 documents
    tables = [h.key for h in hits if h.key.startswith("table:")]
    assert tables and tables[0] == "table:documents", "问文档，最相近的表该是 documents"
    assert any(h.key.startswith("metric:") for h in hits), "口径与表同在一个索引里"
    assert 0.0 <= hits[0].score <= 1.0001
    # 分数必须是降序 —— 上层按顺序取 max_k，顺序错了阈值判定跟着错
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_index_lives_in_the_database_not_in_the_process(cfg, vec_store, fake_embed):
    """换掉 Chroma 的头一条理由：另一个副本要能直接用上这份索引。

    模拟法是把进程内那点缓存全清掉（等价于另起一个 Pod），再查一次 ——
    不应该再调一次 embed_documents，因为向量已经在库里了。
    """
    vectors.VectorIndex(cfg).search("文档", 2)
    assert fake_embed.docs_calls == 1

    vectors.reset_cache()                      # 等价于"换一个副本"
    hits = vectors.VectorIndex(cfg).search("文档", 2)
    assert hits
    assert fake_embed.docs_calls == 1, "索引在库里，第二个副本不该重新嵌入一遍"


def test_fingerprint_change_reindexes_and_collects_the_old_one(
        cfg, vec_store, fake_embed):
    """表注释改了就是另一份 schema：必须重建，且旧的那份不能留在表里。

    留着旧指纹不会答错（读的是新 collection），但这张表会随着"每改一次注释"
    单调增长，且没有任何人会去清它。
    """
    first = vectors.VectorIndex(cfg)
    first.search("文档", 2)

    next(iter(cfg.tables.values())).desc += "（补一句注释）"
    second = vectors.VectorIndex(cfg)
    assert second.collection != first.collection, "指纹应随 schema 变化"
    second.search("文档", 2)

    rows = pgstore.rows("SELECT DISTINCT collection FROM askdb_schema_vectors")
    assert [r[0] for r in rows] == [second.collection], "同一个源的旧指纹应被收掉"


def test_two_sources_keep_separate_indexes(cfg, vec_store, fake_embed):
    """收旧指纹只在**同一个源**内进行 —— 收过头就是把别的源的索引删了。"""
    import dataclasses

    a = dataclasses.replace(cfg, source_id="src_aaaaaaaaaaaa")
    b = dataclasses.replace(cfg, source_id="src_bbbbbbbbbbbb")
    vectors.VectorIndex(a).search("文档", 2)
    vectors.VectorIndex(b).search("文档", 2)

    rows = pgstore.rows("SELECT DISTINCT space FROM askdb_schema_vectors")
    assert {r[0] for r in rows} == {"src_aaaaaaaaaaaa", "src_bbbbbbbbbbbb"}


# ------------------------------------------------------------------ 走不通的那些路

def test_missing_extension_says_so(cfg, vec_store, fake_embed, monkeypatch):
    """pgvector 没装是**部署问题**，必须把处置办法说出来。

    这条是这轮改造的核心取舍：宁可报一句看得懂的话，也不要像换之前那样
    —— 依赖缺失被兜成一句 note，线上跑了两天没人知道。
    """
    monkeypatch.setattr(vectors, "_vec_ns", None)
    monkeypatch.setattr(pgstore, "rows", lambda *a, **k: [])
    with pytest.raises(vectors.EmbeddingUnavailable) as e:
        vectors.VectorIndex(cfg).search("文档", 2)
    assert "pgvector" in str(e.value) and "CREATE EXTENSION" in str(e.value)


def test_store_down_falls_back_instead_of_exploding(cfg, vec_store, fake_embed,
                                                    monkeypatch):
    """向量库连不上只该让召回退化，不该让问答链路 500。"""
    def boom(*a, **k):
        raise pgstore.StoreUnavailable("凭据库连接失败：connection refused")

    monkeypatch.setattr(pgstore, "rows", boom)
    monkeypatch.setattr(pgstore, "connect", boom)
    with pytest.raises(vectors.EmbeddingUnavailable) as e:
        vectors.VectorIndex(cfg).search("文档", 2)
    assert "向量库不可用" in str(e.value)


def test_no_api_key_is_reported_with_the_variable_name(cfg, vec_store, monkeypatch):
    """没配密钥时要给出**变量名** —— 部署方据此知道去配哪一个。"""
    monkeypatch.setattr(type(cfg), "api_key", lambda self: "", raising=False)
    with pytest.raises(vectors.EmbeddingUnavailable) as e:
        vectors.VectorIndex(cfg).search("文档", 2)
    assert cfg.llm["api_key_env"] in str(e.value)


def test_vector_literal_round_trips_through_pg(cfg, vec_store, fake_embed):
    """传进去的向量必须原样落库 —— 文本格式写错会静默变成另一个向量。"""
    vectors.VectorIndex(cfg).search("文档", 1)
    rows = pgstore.rows(
        "SELECT dim, embedding::text FROM askdb_schema_vectors LIMIT 1")
    dim, text = rows[0]
    assert dim == 3
    assert text.startswith("[") and text.count(",") == 2


def test_reset_cache_clears_the_extension_schema_too(cfg, vec_store, fake_embed):
    """扩展所在 schema 也是**跟着库走**的缓存，换库不清就会指到别处。

    漏了这一处的实际表现：换一个库（或换 schema）之后，建表语句里写的还是
    上一个库的 schema 名，报 "schema … does not exist" —— 而扩展明明装着，
    错误信息把人指向完全错误的方向。
    """
    vectors.VectorIndex(cfg).search("文档", 1)
    assert vectors._vec_ns, "查过一次之后应缓存下来"
    vectors.reset_cache()
    assert vectors._vec_ns is None
