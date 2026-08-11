"""Schema 向量召回（技术设计说明书 §3.2.3）。

与关键词召回共用同一份文档构造逻辑（schema_rag.table_doc / metric_doc），
换实现时**注入提示词的内容不变** —— 消融实验才有可比性。

嵌入模型走 OpenAI 兼容端点。取不到密钥、或维度对不上时不抛异常，
而是让调用方回落到关键词召回：召回退化只是准确率下降，
不该让整条链路不可用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .config import Config


class EmbeddingUnavailable(RuntimeError):
    """嵌入服务不可用 —— 调用方应回落到关键词召回。"""


@dataclass
class Hit:
    key: str          # "table:documents" / "metric:卡住的文档"
    score: float      # 余弦相似度，越大越相关


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


class VectorIndex:
    """基于 Chroma 的持久化索引。首次使用时按需构建。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._col: Any = None
        self._embed: Any = None

    # ---------- 嵌入 ----------

    def _embedder(self):
        if self._embed is not None:
            return self._embed
        key = self.cfg.api_key()
        if not key:
            raise EmbeddingUnavailable(
                f"未配置 {self.cfg.llm['api_key_env']}，无法生成向量。"
            )
        from langchain_openai import OpenAIEmbeddings

        self._embed = OpenAIEmbeddings(
            model=self.cfg.raw["schema_rag"].get("embedding_model", "text-embedding-v4"),
            base_url=self.cfg.llm["base_url"],
            api_key=key,
            # 默认行为是客户端先分词、发 token id 数组，DashScope 只收字符串，
            # 不关掉会直接 400："contents is neither str nor list of str"
            check_embedding_ctx_length=False,
            # 百炼单次批量有上限，schema 文档不多，小批稳妥
            chunk_size=10,
        )
        return self._embed

    # ---------- 索引 ----------

    def _collection(self):
        if self._col is not None:
            return self._col
        import chromadb

        path = (self.cfg.root / ".chroma").resolve()
        client = chromadb.PersistentClient(path=str(path))
        name = f"schema_{_fingerprint(self.cfg)}"

        existing = {c.name for c in client.list_collections()}
        # 指纹变了说明 schema 或口径改过，旧集合直接弃用（不删，便于回溯）
        col = client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})
        if name not in existing or col.count() == 0:
            self._build(col)
        self._col = col
        return col

    def _build(self, col) -> None:
        from . import schema_rag

        ids, docs = [], []
        for t in self.cfg.tables.values():
            ids.append(f"table:{t.name}")
            docs.append(schema_rag.table_doc(t))
        for m in self.cfg.metrics:
            ids.append(f"metric:{m.name}")
            docs.append(schema_rag.metric_doc(m))
        if not ids:
            return
        col.upsert(ids=ids, documents=docs, embeddings=self._embedder().embed_documents(docs))

    # ---------- 查询 ----------

    def search(self, question: str, k: int) -> list[Hit]:
        try:
            col = self._collection()
            vec = self._embedder().embed_query(question)
        except EmbeddingUnavailable:
            raise
        except Exception as e:  # 网络、维度不符、Chroma 异常等
            raise EmbeddingUnavailable(f"向量召回不可用：{str(e).splitlines()[0]}") from e

        res = col.query(query_embeddings=[vec], n_results=min(k, max(col.count(), 1)))
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        # Chroma 的 cosine 距离 = 1 - 相似度
        return [Hit(key=i, score=1.0 - float(d)) for i, d in zip(ids, dists)]
