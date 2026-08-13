"""Schema 与业务口径召回。

设计要点（技术设计说明书 §3.2.3）：
  * **禁止全库注入。** 无关表既浪费 token，又会干扰模型选表。
  * token 预算超出时按相关度截断，并**记录告警** ——
    静默截断会造成不可解释的准确率下降。
  * P0 采用关键词/别名匹配；P1 换向量检索。两种模式共用同一份文档构造逻辑，
    换实现时提示词内容不变，消融实验才有可比性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config, Metric, Table


@dataclass
class Recall:
    tables: list[Table] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    prompt: str = ""
    est_tokens: int = 0
    truncated: list[str] = field(default_factory=list)   # 因预算被裁掉的表名
    mode: str = ""
    note: str = ""                                      # 降级等需要告知的情况

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]


def _est_tokens(text: str) -> int:
    """粗略估算：中文按字符计，英文按 4 字符 1 token。够用于预算控制。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4


def table_doc(t: Table) -> str:
    """把一张表渲染成提示词片段。召回与注入共用，保证两者一致。"""
    lines = [f"表 {t.name} —— {t.desc}"]
    if t.aliases:
        lines.append(f"  别名：{'、'.join(t.aliases)}")
    for c in t.columns.values():
        bits = [f"  - {c.name} ({c.type})"]
        if c.desc:
            bits.append(c.desc)
        if c.enum:
            bits.append(f"取值：{'/'.join(c.enum)}")
        if c.tenant:
            bits.append("【租户隔离列，系统会强制注入，不要自己写】")
        lines.append("  ".join(bits))
    return "\n".join(lines)


def metric_doc(m: Metric) -> str:
    head = f"口径「{m.name}」"
    if m.aliases:
        head += f"（同义：{'、'.join(m.aliases)}）"
    body = m.expr or m.predicate or ""
    out = f"{head}\n  必须使用此定义，不得自行构造：{body}"
    if m.note:
        out += f"\n  说明：{m.note}"
    return out


def _score(t: Table, question: str) -> int:
    """关键词相关度。表名/别名命中权重最高，其次字段名与字段说明。"""
    s = 0
    q = question.lower()
    if t.name.lower() in q:
        s += 10
    for a in t.aliases:
        if a and a in question:
            s += 8
    for c in t.columns.values():
        if c.name.lower() in q:
            s += 3
        if c.desc and any(w and w in question for w in c.desc.split("，")[:1]):
            s += 1
        for e in c.enum:
            if e.lower() in q:
                s += 2
    return s


def _keyword_pick(question: str, cfg: Config, top_k: int, max_k: int) -> list[Table]:
    all_tables = list(cfg.tables.values())
    scored = sorted(((_score(t, question), t) for t in all_tables), key=lambda x: -x[0])
    picked = [t for s, t in scored if s > 0][:max_k]
    if len(picked) < top_k:
        # 召回不足时补齐，宁可多给一张表，也不要让模型无表可用
        for _, t in scored:
            if t not in picked:
                picked.append(t)
            if len(picked) >= top_k:
                break
    return picked


def recall(question: str, cfg: Config, index: Any = None) -> Recall:
    mode = cfg.raw["schema_rag"].get("mode", "keyword")
    budget = int(cfg.raw["schema_rag"].get("token_budget", 1500))
    top_k = int(cfg.raw["schema_rag"].get("top_k", 3))
    max_k = int(cfg.raw["schema_rag"].get("max_k", 5))

    all_tables = list(cfg.tables.values())
    metrics = [m for m in cfg.metrics if m.matches(question)]
    note = ""

    if mode == "all":
        picked = all_tables
    elif mode == "vector":
        from .vectors import EmbeddingUnavailable, VectorIndex

        idx = index or VectorIndex(cfg)
        # 表和口径在同一个索引里，若只取 max_k 条，两者会互相挤占名额 ——
        # 于是多取一些，再各自按配额与阈值筛。
        want = max_k + len(cfg.metrics) + 2
        min_score = float(cfg.raw["schema_rag"].get("min_score", 0.35))
        max_metrics = int(cfg.raw["schema_rag"].get("max_metrics", 2))
        try:
            hits = idx.search(question, want)
        except EmbeddingUnavailable as e:
            # 召回退化只是准确率下降，不该让整条链路不可用
            picked = _keyword_pick(question, cfg, top_k, max_k)
            mode, note = "keyword", f"向量召回不可用，已回落关键词：{e}"
        else:
            ranked = [(h.score, cfg.tables[h.key.split(":", 1)[1]])
                      for h in hits
                      if h.key.startswith("table:") and h.key.split(":", 1)[1] in cfg.tables]
            # 过线的才要，但至少保底 top_k 张。
            # 保底不是妥协，是设计 §3.2.3 的明文要求：「召回 Top-K（默认 3，
            # 上限 5）表」。因此 min_score **不是硬阈值** —— 过线表不足 top_k
            # 时，低于阈值的表会被补齐进来。配置注释已同步说明这一点；
            # 若要让它成为硬阈值，须先改设计文档里的 Top-K 约定。
            picked = [t for s, t in ranked if s >= min_score][:max_k]
            if len(picked) < top_k:
                picked = [t for _, t in ranked[:top_k]]
            # 向量也能召回口径 —— 别名没写全时靠语义补上。
            # 但口径是强约束（"必须用此定义"），塞多了反而会误导模型，
            # 所以只收相似度过线的前若干条。
            for h in hits:
                if not h.key.startswith("metric:") or h.score < min_score:
                    continue
                if sum(1 for _ in metrics) >= max_metrics + len(
                        [m for m in cfg.metrics if m.matches(question)]):
                    break
                name = h.key.split(":", 1)[1]
                m = next((x for x in cfg.metrics if x.name == name), None)
                if m and m not in metrics:
                    metrics.append(m)
            if len(picked) < top_k:
                for t in _keyword_pick(question, cfg, top_k, max_k):
                    if t not in picked:
                        picked.append(t)
                    if len(picked) >= top_k:
                        break
    else:
        picked = _keyword_pick(question, cfg, top_k, max_k)

    # 命中口径涉及的表必须一并注入，否则口径表达式引用的列不可见
    by_name = {t.name: t for t in picked}
    for m in metrics:
        for s in m.scope:
            if s not in by_name and s in cfg.tables:
                picked.append(cfg.tables[s])
                by_name[s] = cfg.tables[s]

    # token 预算：超出则按相关度从尾部裁剪，并记录被裁掉的表
    truncated: list[str] = []
    while picked:
        text = _render(picked, metrics)
        if _est_tokens(text) <= budget or len(picked) == 1:
            break
        truncated.append(picked[-1].name)
        picked = picked[:-1]

    prompt = _render(picked, metrics)
    return Recall(
        tables=picked,
        metrics=metrics,
        prompt=prompt,
        est_tokens=_est_tokens(prompt),
        truncated=truncated,
        mode=mode,
        note=note,
    )


def _render(tables: list[Table], metrics: list[Metric]) -> str:
    parts = ["【可用的表】"]
    parts += [table_doc(t) for t in tables]
    if metrics:
        parts.append("\n【业务口径 —— 涉及以下概念时必须使用给定定义】")
        parts += [metric_doc(m) for m in metrics]
    return "\n\n".join(parts)
