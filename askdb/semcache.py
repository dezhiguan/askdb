"""L2/L3：语义缓存 —— 近义问法的收敛。**L3 缓存的是 SQL，不是答案。**

这一层与 L1（qcache）的分工
---------------------------
L1 认的是**问题原文逐字相同**，它有意不做任何规范化（见 qcache 模块头：
规范化会把语义不同的两句并到一条缓存上）。近义问法的收敛留给这里，用向量
而不是字符串规则来判 —— "商品一共多少条""商品总数是多少""统计一下商品数量"
是同一个问题，而没有任何一套大小写/空白规则能把它们并起来。

为什么分成 L2 与 L3 两档
------------------------
askdb 连的是**生产库**：答案有保质期，SQL 没有。所以命中之后有两条路：

  L2 直答   相似度很高（θ_hi）且问题不涉及时间、且缓存还新鲜 → 原样返回上次的
            完整结果。零模型、零配额、零执行，个位数毫秒。
  L3 重跑   相似度够（θ_lo）→ **取出上次那条 SQL 重新执行一遍**，把新数字填回
            上次的结论。零模型调用，只付一条 SQL（实测 15ms 量级）。

L3 才是这一层的主体，理由是 TTL：L2 的答案最多敢存十分钟（陈旧的数字会被当成
当下的数字），而 L3 存的是一条查询计划，它**几天之后依然正确** —— 因为每次都
重新执行。TTL 差两个数量级，命中率就是完全不同的量级。

一条都不能放松的硬约束
----------------------
1. **复用的 SQL 必须重新过一遍 guard 与脱敏。** 走的是 tools.execute_sql 那个
   安全原子，与模型自己发一条 SQL 完全同一条路径。绝不因为"上次过了"就跳过：
   白名单、脱敏列、租户注入、扫描阈值都可能已经变了。
2. **含字面日期的 SQL 不进 L3。** `WHERE day = DATE '2026-07-27'` 复用到下周
   就是在回答上周的问题。判据做成确定性扫描（_plan_reusable），不问模型。
3. **答案只做数值回填，不重新措辞。** 结论里的每一个数字都必须能在上次的结果
   行里找到出处，找不到就判未命中、回落模型链路（_backfill）。这是接地校验
   的反向用法：与其让一段旧文字配一份新数据，不如不命中。
4. **只收干净的成功。** ok、没被任何规则拦、没截断、没降级、接地校验没留下
   查不到出处的数 —— 任何一条不满足都不入库。缓存会把一个错答固化给整个近义簇，
   入库门槛必须比 L1 更严。

三档旋钮
--------
`semantic_cache.mode: off | shadow | enforce`，与 grounding、coverage_check 同一套
语义。**默认 shadow** —— 这一层的命中是一次判定，判错的形态是"给了你另一个
问题的答案"，而且不报错。按项目既定纪律，会改变判定结果的东西一律先影子跑标定：
shadow 下正常跑模型链路，同时把"本来会命中哪一条、相似度多少、L3 能不能回填"
记下来，跑满两轮再定 θ。本机样例想不出真实的误判形状，离线重放不作数。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from . import grounding, pgstore
from .config import Config

_log = logging.getLogger("askdb.semcache")

__all__ = ["Candidate", "lookup", "mode", "remember", "serve_plan", "stats",
           "embed_question", "reset"]

OFF, SHADOW, ENFORCE = "off", "shadow", "enforce"

#: 建表语句。幂等，与 vectors / sources / identity 同一套做法。
#:
#: **另开一张表，不复用 askdb_schema_vectors。** 那张表按 collection 指纹整体
#: 回收（表注释一改就换指纹、旧的成批删掉），把问答缓存混进去会被顺手删光，
#: 而"谁的向量"这件事也就说不清了。
#:
#: scope 是"这个答案在什么条件下才依然成立"的指纹（qcache.scope + 源/组织/角色）。
#: 它进主键：少一维就是跨源/跨身份串结果。
#:
#: 没有 ANN 索引，与 askdb_schema_vectors 同一个判据：单个 scope 就是几百条，
#: 顺序扫描比 ivfflat 又快又准。要加索引的信号是单 scope 上千行。
_DDL = """
CREATE TABLE IF NOT EXISTS askdb_answer_cache (
    scope      text NOT NULL,
    qhash      text NOT NULL,
    question   text NOT NULL,
    dim        integer NOT NULL,
    embedding  {vec} NOT NULL,
    payload    jsonb NOT NULL,
    sql_final  text NOT NULL DEFAULT '',
    plan_ok    boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, qhash)
)
"""

#: 问句里出现这些词，就**不许走 L2 直答**（只可能走 L3 重跑）。
#: 它们共同的性质是"答案随时间变"，而 L2 返回的是一份旧答案 ——
#: 对「今天新增多少」返回十分钟前的数，不是慢一点，是答错了。
_TIME_WORDS = (
    "今天", "今日", "本日", "昨天", "昨日", "明天", "本周", "上周", "本月",
    "上月", "当月", "本季", "本年", "今年", "去年", "近期", "最近", "当前",
    "现在", "目前", "此刻", "实时", "截至", "至今", "今早", "今晚",
)

#: SQL 里出现字面日期 / 字面年份，就不许进 L3 —— 复用到下一周就是在回答上一周的
#: 问题。宁可漏收，不可错复用。
_LITERAL_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}"                 # 2026-07-27
    r"|\d{4}/\d{2}/\d{2}"
    r"|\d{4}\s*年"                       # 2026年
    r"|\bDATE\s*'"                       # DATE '...'
    r"|\bTIMESTAMP\s*'"
    r"|'\d{4}-\d{2}"                     # '2026-07'
    , re.I)


@dataclass
class Candidate:
    """一条近邻。kind 决定它该怎么用。"""
    kind: str                 # "answer"（L2 直答）| "plan"（L3 重跑）
    score: float
    question: str
    payload: dict[str, Any]
    sql: str
    age_s: int
    #: 为什么只能走 plan 而不能直答 —— 影子档要看的正是这个分布。
    why: str = ""


@dataclass
class _Counters:
    lookups: int = 0
    answer_hits: int = 0
    plan_hits: int = 0
    plan_served: int = 0
    plan_backfill_miss: int = 0
    plan_rerun_failed: int = 0
    misses: int = 0
    stored: int = 0
    degraded: str = ""
    shadow: dict[str, int] = field(default_factory=dict)


_c = _Counters()
_lock = threading.Lock()
_ddl_done: set[str] = set()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def _cfg(cfg: Config) -> dict[str, Any]:
    return (cfg.raw.get("semantic_cache") or {})


def mode(cfg: Config) -> str:
    """off / shadow / enforce。**认不出来的值一律当 off**——
    配置写错一个字母就静默启用一个判定层，是最不该发生的方向。"""
    m = str(_cfg(cfg).get("mode", OFF)).strip().lower()
    return m if m in (OFF, SHADOW, ENFORCE) else OFF


def enabled(cfg: Config) -> bool:
    """要不要走这一层的查找。shadow 也要查 —— 它就是靠查出来的东西标定的。"""
    return mode(cfg) != OFF and pgstore.configured()


def _f(cfg: Config, key: str, default: float) -> float:
    try:
        return float(_cfg(cfg).get(key, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 向量
# --------------------------------------------------------------------------
def embed_question(cfg: Config, question: str) -> list[float] | None:
    """这句话的向量。**取不到就返回 None，绝不抛。**

    向量从哪来是这一层成本上唯一要紧的事：链路里 schema 召回本来就要嵌一次
    同一句话，而 L0 把查询向量缓了起来（askdb/l0.py）——于是这里嵌的这一次
    与召回那一次只会真正发一个请求，另一次是进程内命中。**换句话说，
    这一层的 embedding 成本是零。**

    非 vector 模式的源（keyword / all）没有嵌入客户端，这里返回 None，
    整层自动退化为不可用 —— 与"没配 pgvector"同一个处置，stats() 里能看到。
    """
    try:
        from . import vectors
        if str(cfg.raw.get("schema_rag", {}).get("mode", "")) != "vector":
            _note_degraded("数据源未启用向量召回，语义缓存不可用")
            return None
        idx = vectors.get_index(cfg)
        vecs, _tok = idx._embed([question], query=True)   # noqa: SLF001
        return list(vecs[0]) if vecs else None
    except Exception as e:                                # noqa: BLE001
        _note_degraded(f"取问题向量失败：{str(e).splitlines()[0]}")
        return None


def _note_degraded(why: str) -> None:
    with _lock:
        if _c.degraded != why:
            _c.degraded = why
            _log.warning("语义缓存降级：%s", why)


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------
def _ensure(cfg: Config) -> bool:
    """建表。**失败返回 False 而不是抛** —— 缓存不该成为查询失败的原因。"""
    from . import vectors
    try:
        ns = vectors._vector_type()                       # noqa: SLF001
    except Exception as e:                                # noqa: BLE001
        _note_degraded(f"pgvector 不可用：{str(e).splitlines()[0]}")
        return False
    if ns in _ddl_done:
        return True
    try:
        pgstore.execute(_DDL.format(vec=ns))
    except Exception as e:                                # noqa: BLE001
        _note_degraded(f"建表失败：{str(e).splitlines()[0]}")
        return False
    _ddl_done.add(ns)
    return True


def scope_of(cfg: Config, *, org_id: int, role: str) -> str:
    """与 qcache 的 key 同一批维度，少了问题原文那一维。

    复用 qcache.scope 而不是另写一份：两层对"什么条件下这个答案还成立"的
    判断必须逐字相同，否则会长出"L1 认为过期了、L2 还认为没过期"。
    """
    from .qcache import scope as _scope
    raw = "\x00".join([
        _scope(cfg), cfg.source_id or "builtin", str(org_id), role or "ANON",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# 查找
# --------------------------------------------------------------------------
def lookup(cfg: Config, question: str, vec: list[float] | None, *,
           org_id: int, role: str) -> Candidate | None:
    """最近邻。**任何异常都当未命中** —— 这一层是优化，不是护栏。"""
    if not vec or not _ensure(cfg):
        return None
    from . import vectors
    with _lock:
        _c.lookups += 1
    try:
        vt = vectors._vector_type()                       # noqa: SLF001
        dist = f"{vectors._vector_ns()}.cosine_distance"  # noqa: SLF001
        lit = vectors._vec_literal(vec)                   # noqa: SLF001
        rows = pgstore.rows(
            f"SELECT question, payload, sql_final, plan_ok,"
            f" 1 - {dist}(embedding, %s::{vt}) AS score,"
            f" EXTRACT(EPOCH FROM (now() - created_at))::int AS age"
            " FROM askdb_answer_cache WHERE scope = %s"
            f" ORDER BY {dist}(embedding, %s::{vt}) LIMIT %s",
            (lit, scope_of(cfg, org_id=org_id, role=role), lit,
             max(1, int(_f(cfg, "probe_limit", 5)))),
        )
    except Exception as e:                                # noqa: BLE001
        _note_degraded(f"检索失败：{str(e).splitlines()[0]}")
        return None
    if not rows:
        with _lock:
            _c.misses += 1
        return None

    th_ans = _f(cfg, "answer_threshold", 0.82)
    th_plan = _f(cfg, "plan_threshold", 0.75)
    ttl_ans = _f(cfg, "answer_ttl_seconds", 600)
    ttl_plan = _f(cfg, "plan_ttl_seconds", 86400)
    timed = _is_time_sensitive(question)

    best = rows[0]
    q, payload, sql, plan_ok, score, age = (
        str(best[0]), best[1], str(best[2] or ""), bool(best[3]),
        float(best[4]), int(best[5]))
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except Exception:                                 # noqa: BLE001
            return None

    # L2：门槛最高的一档。三个条件缺一不可，缺哪个都记在 why 里 ——
    # 影子档要靠这个分布判断 θ 该往哪边挪。
    # **两条路都要过这一道。** 相似度只负责把候选缩小到一条，
    # 真正决定"是不是同一个问题"的是内容词（见 lexically_same 的实测）。
    same = lexically_same(question, q)
    if not same:
        _note_reject(score, "内容词不同")
        with _lock:
            _c.misses += 1
        return None

    if score >= th_ans and not timed and age <= ttl_ans:
        with _lock:
            _c.answer_hits += 1
        return Candidate("answer", score, q, payload, sql, age)

    why = ("问题涉及时间" if timed else
           "超过直答 TTL" if age > ttl_ans else
           f"相似度 {score:.3f} 未达直答门槛 {th_ans}")
    # L3：够得着就复用 SQL 重跑。这条路对时间敏感的问题**同样成立** ——
    # 它每次都真去查，答的就是当下。
    if score >= th_plan and plan_ok and sql and age <= ttl_plan:
        with _lock:
            _c.plan_hits += 1
        return Candidate("plan", score, q, payload, sql, age, why)

    _note_reject(score, "相似度低于重跑门槛" if score < th_plan else
                 "这条计划不可复用（含字面日期）" if not plan_ok else "超过重跑 TTL")
    with _lock:
        _c.misses += 1
    return None


def _note_reject(score: float, why: str) -> None:
    """**近邻够不着时也要留痕。** 影子档的全部意义是标定，而只记命中的话，
    "差一点就命中"的那一片分布一个数都拿不到 —— θ 就只能继续靠拍。

    分数按 0.05 分桶，理由同上：要的是分布形状，不是每一条的精确值。
    """
    bucket = f"{int(score * 20) / 20:.2f}"
    with _lock:
        _c.shadow[f"reject:{why}"] = _c.shadow.get(f"reject:{why}", 0) + 1
        _c.shadow[f"top1:{bucket}"] = _c.shadow.get(f"top1:{bucket}", 0) + 1


def _is_time_sensitive(question: str) -> bool:
    return any(w in question for w in _TIME_WORDS)


#: 功能词。去掉它们之后剩下的就是"这个问题在问什么"。
#:
#: **只收数量词、疑问词、量词与套话**，绝不收任何能区分问题的词 ——
#: 「数量」「销售额」「新增」「在售」「本月」都必须留着，它们正是判据要看的。
#: 按长度倒序替换，否则「一共」会先被「共」吃掉半截。
_STOP_WORDS = sorted([
    "请问", "我想知道", "帮我", "给我", "查一下", "看一下", "统计一下", "统计",
    "查询", "一下", "总共有", "总共", "一共有", "一共", "总数", "总量", "数目",
    "加起来", "分别是", "分别", "各自", "是多少", "有多少", "多少", "几条",
    "几个", "多少条", "的数量", "条记录", "行数据", "记录数", "记录", "数据",
    "表里", "表中", "表", "里", "中", "有", "是", "了", "吗", "呢", "啊",
    "的", "条", "个", "行", "共", "请", "把", "来", "下",
], key=len, reverse=True)

_PUNCT = re.compile(r"[\s，。？?、,.!！:：;；\"'“”‘’()（）【】\[\]]")


def _content(question: str) -> frozenset[str]:
    """一句提问的**内容词集合**：去掉功能词，剩下的切成中文二元组 / 英文整词。

    切二元组而不是分词，是因为这套部署里没有中文分词器，而二元组对本判据
    足够 —— 它要判的不是"这句话讲了什么"，只是"两句话剩下的字是不是同一批"。
    """
    s = _PUNCT.sub("", question.lower())
    for w in _STOP_WORDS:
        s = s.replace(w, "|")
    out: set[str] = set()
    for part in (p for p in s.split("|") if p):
        if re.fullmatch(r"[0-9a-z_]+", part) or len(part) == 1:
            out.add(part)
        else:
            out.update(part[i:i + 2] for i in range(len(part) - 1))
    return frozenset(out)


def lexically_same(a: str, b: str) -> bool:
    """两句提问在**内容词**上是不是同一个问题。相似度只缩小候选，这里才是判据。

    为什么必须有这一道 —— 2026-09-15 用 text-embedding-v4 实测 13 组问法对
    （真实模型、真实问法），两个分布**重叠**：

        0.9083  换了实体   商品总共有多少条记录 ⇄ 订单总共有多少条记录
        0.8888  同义       会员总数是多少     ⇄ 一共有多少会员
        0.8806  换时间窗   本月新增商品数     ⇄ 上月新增商品数
        0.8508  换了聚合   各品类商品数量分布 ⇄ 各品类商品销售额分布
        0.7552  同义       商品总共有多少条记录 ⇄ 统计一下商品的数量

    同义 0.755–0.889，会答错的那批 0.596–0.908 —— **最危险的那一对比所有同义对
    都高**。也就是说没有任何一条 θ 能把两者分开：调高则一条都不命中，调低就会
    拿订单的数去答商品。方案 V1.2 里那两个 θ（0.93 / 0.88）是拍的，实测把它们
    证伪了；而证伪的不只是取值，是"单靠相似度"这个做法本身。

    这一道判据在同一份探针上 12/13，且**八条危险对全部拦下**。唯一一处错判
    （「各品类商品数量分布」⇄「按品类统计商品数量」被拦）是漏收，不是错答 ——
    方向是对的：漏收只是多花一次钱，错答是给出一个错的数字且不报错。
    """
    return _content(a) == _content(b)


# --------------------------------------------------------------------------
# L3：复用 SQL 重跑
# --------------------------------------------------------------------------
def serve_plan(cfg: Config, cand: Candidate, *, org_id: int,
               executor: Any = None) -> dict[str, Any] | None:
    """把一条 plan 候选兑现成本次的应答。**兑现不了就返回 None，回落模型链路。**

    兑现失败有两种，都要记数：SQL 这次没跑成（库变了、列没了、被护栏拦了），
    或者数值回填对不上（结果形状变了）。两者在影子档里要分得开 —— 前者说明
    这条计划该淘汰，后者说明 θ 或回填判据该收紧。
    """
    from . import tools

    res = tools.execute_sql(cand.sql, cfg, org_id, executor=executor)
    if not res.ok:
        with _lock:
            _c.plan_rerun_failed += 1
        return None

    d = res.data
    old = cand.payload
    new_rows = list(d.get("rows") or [])
    old_rows = list(old.get("rows") or [])

    text = str(old.get("reasoning") or "")
    filled = _backfill(text, old_rows, new_rows)
    if filled is None:
        with _lock:
            _c.plan_backfill_miss += 1
        return None
    caliber = _backfill(str(old.get("caliber") or ""), old_rows, new_rows)
    if caliber is None:
        caliber = str(old.get("caliber") or "")

    out = dict(old)
    # 结果那一截全部换成这次真跑出来的。**脱敏与截断也必须取本次的** ——
    # 它们描述的是这一次执行发生了什么，沿用上次就是在说一句假话。
    out.update({
        "rows": new_rows, "columns": list(d.get("columns") or []),
        "row_count": int(d.get("row_count") or 0),
        "truncated": bool(d.get("truncated")), "as_of": d.get("as_of") or "",
        "masked_columns": list(d.get("masked_columns") or []),
        "mask_degraded": bool(d.get("mask_degraded")),
        "explain_rows": d.get("explain_rows"),
        "sql_final": d.get("sql_final") or cand.sql,
        "rules_fired": list(d.get("rules_fired") or []),
        "rewrites": list(d.get("rewrites") or []),
        "reasoning": filled, "caliber": caliber,
    })
    with _lock:
        _c.plan_served += 1
    return out


def _backfill(text: str, old_rows: list, new_rows: list) -> str | None:
    """把结论里的数字换成这次跑出来的值。**换不了就返回 None。**

    这是接地校验的反向用法。规矩只有一条：**结论里的每个数字都要有出处。**

      · 能在上次结果行里找到出处 → 换成新结果对应位置的值
      · 找不到出处，但它是序数（不超过行数）或年份 → 原样留着
        （"前三名""第 2 位""2026 年"这类不是从数据里来的）
      · 其余一律判失败 —— 与其让一段旧文字配一份新数据，不如不命中

    形状必须一致才谈得上"对应位置"：行数、列数相同，且**所有非数值单元格
    逐字相同**（分组键没变）。少了这一条，换出来的就是张冠李戴的数字。
    """
    if not text:
        return text
    if len(old_rows) != len(new_rows):
        return None
    for a, b in zip(old_rows, new_rows):
        if len(a) != len(b):
            return None
        for x, y in zip(a, b):
            if grounding._as_float(x) is None:            # noqa: SLF001
                if str(x) != str(y):
                    return None                           # 分组键变了

    # 值相同就不必替换 —— 数据没动，上次那段结论逐字成立。
    if old_rows == new_rows:
        return text

    # 旧值 → 新值。按"格式化后的字符串"建索引：结论里写的是渲染过的数字。
    table: dict[str, str] = {}
    for a, b in zip(old_rows, new_rows):
        for x, y in zip(a, b):
            fx, fy = grounding._as_float(x), grounding._as_float(y)   # noqa: SLF001
            if fx is None or fy is None:
                continue
            for form in _forms(x, fx):
                table.setdefault(form, _same_style(form, y, fy))

    n_rows = len(old_rows)
    out, last = [], 0
    for m in grounding._NUM.finditer(text):                # noqa: SLF001
        tok = m.group(0)
        if tok in table:
            out.append(text[last:m.start()])
            out.append(table[tok])
            last = m.end()
            continue
        val = grounding._as_float(tok.replace(",", ""))     # noqa: SLF001
        if val is None:
            return None
        if float(val).is_integer() and 0 < val <= max(n_rows, 10):
            continue                                       # 序数 / 小计数
        if grounding._year_like(val):                      # noqa: SLF001
            continue
        return None                                        # 查不到出处
    out.append(text[last:])
    return "".join(out)


def _forms(raw: Any, val: float) -> list[str]:
    """一个值在结论里可能被写成的几种样子。"""
    forms = [str(raw)]
    if float(val).is_integer():
        i = int(val)
        forms += [str(i), f"{i:,}"]
    else:
        forms.append(repr(val))
    return [f for f in dict.fromkeys(forms) if f]


def _same_style(old_form: str, raw: Any, val: float) -> str:
    """新值按旧值的写法渲染 —— 旧的带千分位，新的也带。"""
    if "," in old_form and float(val).is_integer():
        return f"{int(val):,}"
    if float(val).is_integer():
        return str(int(val))
    return str(raw)


# --------------------------------------------------------------------------
# 入库
# --------------------------------------------------------------------------
def remember(cfg: Config, question: str, vec: list[float] | None,
             payload: dict[str, Any], *, org_id: int, role: str) -> None:
    """把一次干净的成功记下来。**门槛比 L1 严，理由见模块头第 4 条。**

    不抛任何异常：这是收尾动作，缓存写失败不该把一次已经答完的查询变成错误。
    """
    if not vec or not payload or not _ensure(cfg):
        return
    if not _storable(payload):
        return
    sql = str(payload.get("sql_final") or "")
    plan_ok = _plan_reusable(sql)
    from . import vectors
    try:
        qh = hashlib.sha256(question.encode("utf-8")).hexdigest()
        pgstore.execute(
            "INSERT INTO askdb_answer_cache"
            " (scope, qhash, question, dim, embedding, payload, sql_final,"
            "  plan_ok, created_at)"
            f" VALUES (%s,%s,%s,%s,%s::{vectors._vector_type()},%s,%s,%s,now())"  # noqa: SLF001
            " ON CONFLICT (scope, qhash) DO UPDATE SET"
            " embedding = EXCLUDED.embedding, payload = EXCLUDED.payload,"
            " sql_final = EXCLUDED.sql_final, plan_ok = EXCLUDED.plan_ok,"
            " created_at = now()",
            (scope_of(cfg, org_id=org_id, role=role), qh, question, len(vec),
             vectors._vec_literal(vec),                    # noqa: SLF001
             json.dumps(payload, ensure_ascii=False, default=str),
             sql, plan_ok),
        )
        with _lock:
            _c.stored += 1
        _prune(cfg, org_id=org_id, role=role)
    except Exception as e:                                # noqa: BLE001
        _note_degraded(f"写入失败：{str(e).splitlines()[0]}")


def _storable(payload: dict[str, Any]) -> bool:
    """能不能进这一层。**任何一条不满足就不收。**

    比 L1 多挡的是截断、降级与接地校验留下的疑点 —— L1 的一条记录只服务
    逐字相同的那一句问话，而这里的一条会服务**整个近义簇**：收一个坏答案
    进来，错的就不止一个人、也不止一次。
    """
    if not payload.get("ok") or payload.get("rejected_by"):
        return False
    if payload.get("truncated") or payload.get("mask_degraded"):
        return False
    if payload.get("recall_blind") or payload.get("recall_degraded"):
        return False
    if payload.get("scope_narrowed") or payload.get("ungrounded_numbers"):
        return False
    return bool(payload.get("sql_final") or payload.get("reasoning"))


def _plan_reusable(sql: str) -> bool:
    """这条 SQL 能不能留到以后重跑。见模块头第 2 条。"""
    if not sql.strip():
        return False
    return not _LITERAL_DATE.search(sql)


def _prune(cfg: Config, *, org_id: int, role: str) -> None:
    """按条数封顶。过期的行不单独清 —— 查找时按 TTL 判，过期行只是占位；
    真正会无限长的是条数，所以按条数收。"""
    cap = int(_f(cfg, "max_entries_per_scope", 500))
    if cap <= 0:
        return
    try:
        pgstore.execute(
            "DELETE FROM askdb_answer_cache WHERE scope = %s AND qhash IN ("
            "  SELECT qhash FROM askdb_answer_cache WHERE scope = %s"
            "  ORDER BY created_at DESC OFFSET %s)",
            (scope_of(cfg, org_id=org_id, role=role),
             scope_of(cfg, org_id=org_id, role=role), cap),
        )
    except Exception:                                     # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 观测
# --------------------------------------------------------------------------
def note_shadow(kind: str) -> None:
    """影子档记一次"本来会怎样"。不向用户发声，只进计数与日志。"""
    with _lock:
        _c.shadow[kind] = _c.shadow.get(kind, 0) + 1


def note_shadow_verdict(cand: "Candidate", real: dict[str, Any]) -> str:
    """影子档最要紧的那一维：**如果当时命中了，答得对不对。**

    只记 kind 与相似度是不够的 —— 它只说明"会不会命中"，不说明"命中了会不会
    答错"，而 θ 要靠后者来定。真跑结束之后拿两份结果比一次，分三档：

      agree     —— 缓存里那份与真跑这份说的是同一批数（数字集合相同）
      disagree  —— 数字不同。**这是要盯的那一档**：它意味着若已切 enforce，
                   这次就会返回一个错的数，而且不报错。
      unknown   —— 一边没有数字可比（拒答、纯文字结论），比不出来

    比的是**数字集合**而不是文本：措辞每次都可能不同，而这一层复用的正是
    "同一批数换个说法"。用 grounding 那套数字提取，与接地校验同源，
    不另立一套口径。

    plan 档还要多比一层：它是重跑之后回填的，所以真正该问的是"回填出来的
    那份与真跑的那份一不一致"—— 但重跑要付一条 SQL，影子档不做（影子不该
    产生任何副作用）。所以 plan 在这里比的是**上次那份旧数**与真跑的差异，
    读的时候要记得：它给出的是 plan 档的**上界误差**，实际命中会比这个好。
    """
    a = set(grounding.numbers_in(str(cand.payload.get("reasoning") or "")))
    b = set(grounding.numbers_in(str(real.get("reasoning") or "")))
    verdict = "unknown" if not a or not b else ("agree" if a == b else "disagree")
    with _lock:
        key = f"{cand.kind}_{verdict}"
        _c.shadow[key] = _c.shadow.get(key, 0) + 1
    return verdict


def stats() -> dict[str, Any]:
    with _lock:
        return {
            "lookups": _c.lookups, "answer_hits": _c.answer_hits,
            "plan_hits": _c.plan_hits, "plan_served": _c.plan_served,
            "plan_backfill_miss": _c.plan_backfill_miss,
            "plan_rerun_failed": _c.plan_rerun_failed,
            "misses": _c.misses, "stored": _c.stored,
            "shadow": dict(_c.shadow),
            "degraded": _c.degraded,
        }


def reset() -> None:
    """单测用。"""
    global _c
    with _lock:
        _c = _Counters()
    _ddl_done.clear()
