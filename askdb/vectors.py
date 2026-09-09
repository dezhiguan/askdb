"""Schema 向量召回（技术设计说明书 §3.2.3）。

与关键词召回共用同一份文档构造逻辑（schema_rag.table_doc / metric_doc），
换实现时**注入提示词的内容不变** —— 消融实验才有可比性。

嵌入模型走 OpenAI 兼容端点，向量落 PostgreSQL 的 askdb_schema_vectors 表
（pgvector）。取不到密钥、装不上扩展、库连不上时都不抛到调用方头上，
而是让 schema_rag 回落到关键词召回：召回退化只是准确率下降，
不该让整条链路不可用。**但退化必须留痕** —— 见 schema_rag.note_degraded。

2026-09-09：从 Chroma（嵌入式，落 cfg.root/.chroma）换成 PG。
换掉的三个理由，都是 Chroma 方案在这套部署下**已经**踩着的：

- **索引落在容器本地目录，而线上是三副本。** 每个 Pod 各建各的索引，同一份
  schema 要付三遍 embedding 的钱；rollout 之后全丢，再付一遍。
- **镜像里根本没装。** chromadb 在 `vectors` 这个 optional extra 里，而
  Dockerfile 装的是 `.[web,redis,postgres]`。于是配置写着 mode: vector、
  运行时每一次请求都回落 keyword，从 2026-09-07 切过来之后一直如此，
  唯一的线索是单次结果里那行 recall_note。存储进了 PG，这条路上就没有
  "装没装"这个变量了 —— psycopg 是主依赖。
- 30 来张表的规模，本来就不需要一个独立的向量引擎。

为什么用 pgvector 而不是 float8[] 自己在 Python 里算余弦
--------------------------------------------------------
距离计算下推给库，读回来的就是 Top-K 而不是整张表；将来表多到需要
ivfflat/hnsw 时只是加一条 CREATE INDEX，读法一个字不用改。代价是
askdb_meta 上要有 vector 扩展 —— 装不上时**明确报出来**，
由 schema_rag 记成一次降级，而不是悄悄退回全表扫描。

不引 pgvector-python：向量以 '[1,2,3]' 的文本形式传参、在库里转成
vector，一个依赖都不用加。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Any

from . import pgstore
from .config import Config

log = logging.getLogger("askdb.vectors")


class EmbeddingUnavailable(RuntimeError):
    """向量召回这条路走不通 —— 调用方应回落到关键词召回。

    嵌入服务不可用、向量库连不上、pgvector 装不上，三种都归它：
    对调用方来说处置完全一样（回落 + 记降级），差别只在 message 里，
    而那句 message 会一路带到界面上。
    """


@dataclass
class Hit:
    key: str          # "table:documents" / "metric:卡住的文档"
    score: float      # 余弦相似度，越大越相关


#: 建表语句。幂等，与 sources / identity 同一套做法（省掉一套迁移工具）。
#:
#: collection 是 schema+口径+模型的指纹，space 是"这份索引属于哪个数据源"。
#: 两个都要：指纹变了要换一份新的（否则召回的是旧结构），而旧的那份必须能被
#: 收掉 —— 靠 space 找得到同一个源的历史指纹，否则每改一次表注释就在表里
#: 留一堆再也没人读的向量。
#:
#: **space 进主键**，于是两个 schema 完全相同的源各存一份（多花一次嵌入，
#: 几十条文档而已）。合存看着更省，但回收就不再安全：A 改了表注释、按
#: space=A 收掉旧指纹时，会把 B 正用着的那份一起删掉 —— B 下次查询要重建，
#: 而"谁的索引"这件事从此说不清。省那几十行不值得换来一个说不清的所有权。
#:
#: embedding 不写维度：同一套部署可能换嵌入模型（text-embedding-v4 是 1024 维，
#: 换一个就是别的数），写死维度会让换模型变成一次改表。维度不一致时
#: 距离函数会在库里报错，而那正好是"指纹该换了"的信号，由 search() 兜成降级。
#:
#: **没有 ANN 索引，是有意的。** 单个 collection 就是白名单里那几十条文档，
#: 顺序扫描比 ivfflat 快也更准；ivfflat 在几十行上还会因为 lists 太小而
#: 召回不全。要加索引的信号是单个 collection 上千行，不是"看起来该有个索引"。
_DDL = """
CREATE TABLE IF NOT EXISTS askdb_schema_vectors (
    collection text NOT NULL,
    space      text NOT NULL DEFAULT '',
    key        text NOT NULL,
    dim        integer NOT NULL,
    embedding  {vec} NOT NULL,
    PRIMARY KEY (collection, space, key)
)
"""

#: pgvector 装在哪个 schema。**类型与距离函数都必须带上它。**
#:
#: 池子在 ASKDB_STORE_SCHEMA 非 public 时会 `SET search_path TO <schema>`，
#: 那一句把 public 挤出了搜索路径 —— 而扩展通常装在 public。于是不限定
#: schema 的 `::vector` 报 "type vector does not exist"、`<=>` 报
#: "operator does not exist"，而扩展明明装着。一次查目录问清楚它在哪，
#: 比让每个部署自己去调 search_path 靠谱。
#:
#: 距离用函数 `cosine_distance(a, b)` 而不是操作符 `<=>`：**操作符没法限定
#: schema**（PostgreSQL 的 `OPERATOR(public.<=>)` 写法可用但极易写错，且
#: 拼进 SQL 更难读），函数加个前缀就完事。语义完全相同。
_vec_ns: str | None = None


def _vector_ns() -> str:
    """pgvector 所在 schema（已加引号）。扩展没装时**明确报出来**，不猜。"""
    global _vec_ns
    if _vec_ns:
        return _vec_ns
    rows = pgstore.rows(
        "SELECT n.nspname FROM pg_extension e"
        " JOIN pg_namespace n ON n.oid = e.extnamespace"
        " WHERE e.extname = 'vector'"
    )
    if not rows:
        raise EmbeddingUnavailable(
            "askdb 元数据库上没有 pgvector 扩展：在该库执行一次"
            " CREATE EXTENSION vector（需要管理员权限）。"
        )
    _vec_ns = f'"{rows[0][0]}"'
    return _vec_ns


def _vector_type() -> str:
    return f"{_vector_ns()}.vector"


def _fingerprint(cfg: Config) -> str:
    """schema + 口径 + 模型 的指纹。

    任何一处变了，索引就必须重建 —— 否则召回的是旧结构，
    而这种错误在结果上表现为"莫名其妙查不准"，极难定位。
    """
    from . import schema_rag

    parts = [cfg.raw["schema_rag"].get("embedding_model", "")]
    parts += [schema_rag.table_doc(t) for t in cfg.tables.values()]
    parts += [schema_rag.metric_doc(m) for m in cfg.metrics]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _space(cfg: Config) -> str:
    """这份索引属于哪个数据源。运行时源用 id，内置源用配置路径。"""
    return cfg.source_id or cfg.path


def _vec_literal(vec: list[float]) -> str:
    """pgvector 的文本输入格式。用它就不必引 pgvector-python。"""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


#: 已经确认建好的 collection。**只是省掉一次 count 查询**，不是正确性的一部分：
#: 进程重启、或另一个副本先建好，都只是多跑一次幂等的建表 + upsert。
_built: set[tuple[str, str]] = set()
_built_lock = threading.Lock()

#: 嵌入客户端按 (模型, 端点, 密钥) 复用。原来它挂在 VectorIndex 实例上，
#: 而 recall() 每次请求现造一个 VectorIndex —— 于是每一次问答都要重新
#: 构造一次 OpenAIEmbeddings，缓存写了等于没写。
_embedders: dict[tuple[str, str, str], Any] = {}
_embed_lock = threading.Lock()


class VectorIndex:
    """落在 PostgreSQL（pgvector）里的 schema 索引。首次使用时按需构建。

    实例本身不持有状态，构造它不连库也不建客户端 —— 每请求现造一个也不会
    多付代价（真正该复用的嵌入客户端与"建过了"的记录都在模块级）。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.collection = f"schema_{_fingerprint(cfg)}"
        self.space = _space(cfg)

    # ---------- 嵌入 ----------

    def _embedder(self):
        key = self.cfg.api_key()
        if not key:
            raise EmbeddingUnavailable(
                f"未配置 {self.cfg.llm['api_key_env']}，无法生成向量。"
            )
        model = self.cfg.raw["schema_rag"].get("embedding_model", "text-embedding-v4")
        base_url = self.cfg.llm["base_url"]
        ck = (model, base_url, key)
        with _embed_lock:
            hit = _embedders.get(ck)
        if hit is not None:
            return hit
        try:
            from langchain_openai import OpenAIEmbeddings
        except ImportError as e:                       # pragma: no cover
            raise EmbeddingUnavailable(f"未安装 langchain-openai：{e}") from e

        emb = OpenAIEmbeddings(
            model=model,
            base_url=base_url,
            api_key=key,
            # 默认行为是客户端先分词、发 token id 数组，DashScope 只收字符串，
            # 不关掉会直接 400："contents is neither str nor list of str"
            check_embedding_ctx_length=False,
            # 百炼单次批量有上限，schema 文档不多，小批稳妥
            chunk_size=10,
        )
        with _embed_lock:
            _embedders[ck] = emb
        return emb

    # ---------- 索引 ----------

    def _ensure_schema(self) -> None:
        """建扩展 + 建表。幂等。

        扩展装不上是**这台实例的部署问题**，不是用户的问题，所以把原话带出去：
        `CREATE EXTENSION vector` 要么扩展没装在机器上（apt/yum 装 pgvector），
        要么当前账号权限不够。两种都得人去处置，藏起来只会让"切了 vector 却
        一直在跑 keyword"再发生一次。
        """
        with pgstore.connect() as con:
            try:
                con.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as e:
                # 装不上就装不上（多半是权限），但扩展可能**已经**装好了 ——
                # 这里不早退，交给 _vector_type() 按目录判定：以"到底有没有"
                # 为准，而不是以"这条 DDL 跑没跑通"为准。
                log.info("CREATE EXTENSION vector 未执行成功：%s",
                         str(e).splitlines()[0])
            con.execute(_DDL.format(vec=_vector_type()))

    def _build(self) -> None:
        """把当前 schema 的文档嵌入并写进库，顺手收掉同一个源的旧指纹。

        upsert 而不是先删后插：两个副本可能同时在建同一份索引，
        先删后插会让另一个副本在中间那一刻查到半份。
        """
        from . import schema_rag

        keys, docs = [], []
        for t in self.cfg.tables.values():
            keys.append(f"table:{t.name}")
            docs.append(schema_rag.table_doc(t))
        for m in self.cfg.metrics:
            keys.append(f"metric:{m.name}")
            docs.append(schema_rag.metric_doc(m))
        if not keys:
            return

        self._ensure_schema()
        vecs = self._embedder().embed_documents(docs)
        with pgstore.connect() as con:
            with con.transaction():
                for k, v in zip(keys, vecs):
                    con.execute(
                        "INSERT INTO askdb_schema_vectors"
                        " (collection, space, key, dim, embedding)"
                        f" VALUES (%s, %s, %s, %s, %s::{_vector_type()})"
                        " ON CONFLICT (collection, space, key) DO UPDATE"
                        " SET embedding = EXCLUDED.embedding, dim = EXCLUDED.dim",
                        (self.collection, self.space, k, len(v), _vec_literal(v)),
                    )
                # 同一个源的旧指纹到此为止。留着它们没有任何读路径会用到，
                # 只会让这张表随着"每改一次表注释"单调增长。
                con.execute(
                    "DELETE FROM askdb_schema_vectors"
                    " WHERE space = %s AND collection <> %s",
                    (self.space, self.collection),
                )

    def _ensure_built(self) -> None:
        ck = (self.collection, self.space)
        with _built_lock:
            if ck in _built:
                return
        rows = pgstore.rows(
            "SELECT count(*) FROM askdb_schema_vectors"
            " WHERE collection = %s AND space = %s",
            (self.collection, self.space),
        )
        if not rows or not rows[0][0]:
            self._build()
        with _built_lock:
            _built.add(ck)

    # ---------- 查询 ----------

    def search(self, question: str, k: int) -> list[Hit]:
        """按余弦相似度取 Top-K。

        任何一步不通都折成 EmbeddingUnavailable：调用方要做的事只有一件
        （回落关键词并记降级），分不同异常出去只是让每个调用点各写一遍。
        """
        try:
            self._ensure_built()
            vec = self._embedder().embed_query(question)
        except EmbeddingUnavailable:
            raise
        except pgstore.StoreUnavailable as e:
            raise EmbeddingUnavailable(f"向量库不可用：{str(e).splitlines()[0]}") from e
        except Exception as e:                         # 网络、端点报错等
            raise EmbeddingUnavailable(f"向量召回不可用：{str(e).splitlines()[0]}") from e

        lit = _vec_literal(vec)
        try:
            vt, dist = _vector_type(), f"{_vector_ns()}.cosine_distance"
            rows = pgstore.rows(
                f"SELECT key, 1 - {dist}(embedding, %s::{vt}) AS score"
                " FROM askdb_schema_vectors"
                " WHERE collection = %s AND space = %s"
                f" ORDER BY {dist}(embedding, %s::{vt}) LIMIT %s",
                (lit, self.collection, self.space, lit, max(int(k), 1)),
            )
        except Exception as e:
            # 维度对不上（换过嵌入模型）也落在这里 —— 报错原话带出去，
            # 它恰好是"该重建索引了"的信号
            raise EmbeddingUnavailable(
                f"向量检索失败：{str(e).splitlines()[0]}") from e
        return [Hit(key=str(r[0]), score=float(r[1])) for r in rows]


def get_index(cfg: Config) -> VectorIndex:
    """取这份配置对应的索引。构造很轻，真正要复用的东西都在模块级。"""
    return VectorIndex(cfg)


def reset_cache() -> None:
    """丢掉进程内那三份缓存（建过了 / 嵌入客户端 / 类型名）。

    测试换库换 schema 时必须调 —— 类型名跟着库走，跨库复用会指到别处。
    """
    global _vec_ns
    with _built_lock:
        _built.clear()
    with _embed_lock:
        _embedders.clear()
    _vec_ns = None
