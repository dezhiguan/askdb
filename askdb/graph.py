"""LangGraph 状态机 —— 链路主干。

选择状态机而非线性链的原因：存在条件分支（校验失败回边）与重试计数，
且需支持检查点以便失败复现（技术设计说明书 §5）。

**状态必须全部可序列化。** 运行时依赖（配置、模型客户端、执行器、追踪器）
一律走 configurable 传入，不进状态 —— 否则检查点存不下，
"失败样本可原样复现"这条就落不了地，P3 的失败归因也就无从谈起。

P0 为单步链路；plan / assess 两个节点与重规划回边在 P5 补齐，
届时只需新增节点与条件边，现有节点不动。
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from . import clarify, guard, observe, planner, schema_rag
from .config import Config
from .executor import DataSourceError, Executor, MaskUnresolved
from .llm import LlmClient, LlmNotConfigured
from .quota import QuotaExceeded, build_quota
from .audit import MODEL_STEPS, PHASE_STARTED, RESULT_PREVIEW_ROWS, day_tz
from .trace import Tracer, now_iso, write_audit


class AskState(TypedDict, total=False):
    """全部字段可序列化 —— 检查点里存的就是这些。"""

    question: str
    org_id: int
    trace_id: str
    max_retry: int

    schema_prompt: str
    tables_hit: list[str]
    metrics_hit: list[str]
    recall_truncated: list[str]
    #: 召回是盲选（没有任何表命中关键词）/ 召回过程中要告知用户的话。
    #: 进 State 是因为它必须跟着检查点走：续跑一条中断的线程时，
    #: 那句"这次是盲选"不能在恢复后凭空消失。
    recall_blind: bool
    recall_note: str
    #: 召回**降级**过（配置声明 vector，实际跑的是 keyword）。与盲选分开：
    #: 盲选是"一张都没命中"，降级是"换了一套更粗的召回"—— 后者照样能挑出
    #: 几张表来，答案于是长得完全正常，而挑错的概率高得多。
    recall_degraded: bool

    sql_raw: str
    sql_final: str
    reasoning: str
    rules_fired: list[str]
    rewrites: list[str]
    #: 护栏放行了、但读结果的人必须知道的话（R-24 的写死日期提醒等）。
    #: 与 rewrites 分开的理由见 guard.GuardResult.notes。
    guard_notes: list[str]
    #: 结果为空 / 单行聚合全为零，且查询带时间过滤。"这一天没有数据"与
    #: "这一天确实是 0"在数值上完全一样，不说出来读的人分不开。
    empty_note: str

    columns: list[str]
    rows: list[list[Any]]
    mask_degraded: bool
    masked_columns: list[str]
    row_count: int
    truncated: bool
    as_of: str            # 数据时间，来自数据源时钟（§8 准入条件 #7）
    explain_rows: int | None

    error: str | None
    error_hint: str
    rejected_by: str | None
    attempt: int
    #: R-11 把某一版 SQL 拦下过。留着它是为了在重试成功后能说出
    #: "这个数不是全量算出来的" —— 详见 scope_narrowed。
    scan_blocked_sql: str
    scan_blocked_rows: int | None
    #: 本次结果的范围被收窄过：原查询因扫描量超阈值被拦，模型据回灌的提示
    #: 自行加了过滤条件才跑通。**这条必须一路出到接口**：链路上每一步都成功，
    #: rejected_by 是 null，界面与全量结果长得一模一样，而数值可能差两个数量级。
    scope_narrowed: bool
    # 本次拒绝是否属「问题超出范围」。路由据此决定要不要进反思。
    out_of_scope: bool
    # 执行报错是否可由重试救回（超时可以，连接不可达不行）
    exec_retryable: bool
    #: 问句是纯指代追问（clarify 节点判定）。产品上没有多轮上下文，
    #: 这类问题只能澄清，不能猜一个主体把答案编出来。
    anaphoric: bool
    #: 发起人事后补上的条件（clarify 节点的出口）。
    #:
    #: **它不是对话历史，是这一次执行的一个输入。** 产品上没有多轮上下文，
    #: 这条也不会变成"上一轮"—— 它随检查点走，只作用于这条线程，续跑时仍在。
    #: clarify 判指代追问、generate 判信息不足，两处此前都只能终止链路并让人
    #: "换个问法重来"；换个问法就是换一条线程，原来那条永远停在等待补充。
    #: 有了这个字段，补充就回到**同一条线程**上：审计里看得出这是第 2 次执行，
    #: 而不是一条无关的新提问。
    clarification: str
    #: 推理里出现的猜测措辞。**必须一路出到接口并进可信度分母** ——
    #: 模型自认不确定却照样给结果，是这套系统里最难被发现的一类错。
    hedge_terms: list[str]
    #: 从推理里抹掉的假陈述（虚构的"沿用上一轮"、护栏行为的转述）。
    scrubbed_claims: list[str]
    #: 本次 SQL 用到的缓存/派生计数列（如 knowledge_bases.doc_count）。
    derived_columns: list[str]
    #: 本次结果的口径声明。**必出字段**：模型没给就由链路按事实合成。
    caliber: str
    # assess 判"不足"时给出的下一步目标。设计图上 [8] 判不足必然回到 [2]，
    # 没有"重规划反悔"这条边 —— 模型若在 [2] 给不出目标，就用这个兜底。
    next_goal: str

    # ---- 多步规划（§5.3）。同样必须可序列化，检查点要存下来 ----
    multi_step: bool
    goal: str
    step_no: int
    max_steps: int
    steps_done: list[dict[str, Any]]
    carry: dict[str, list]
    enough: bool
    cost_cap_tokens: int
    # R-17 的持久计数：累计 token 记在状态里、随检查点落盘。
    # Tracer 是进程内对象，恢复时新建、计数归零 —— 以它判 R-17
    # 等于给「中断→恢复」留一条绕过成本上限的后门（中断恢复设计 §4.1）。
    tok_used: int
    converged_early: str


@dataclass
class Deps:
    """运行时依赖。经 configurable 传入，不进状态。"""

    cfg: Config
    llm: LlmClient
    executor: Executor
    tracer: Tracer


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def _spent(state: AskState, usage) -> dict[str, Any]:
    """把本次模型消费累进状态（R-17 持久计数）。

    每个花 token 的节点在 tracer.add 之外，还必须把增量并进返回值 ——
    状态进检查点，续跑时自然回种，反复「中断→恢复」也突破不了上限。
    """
    return {"tok_used": int(state.get("tok_used", 0))
            + usage.input_tokens + usage.output_tokens}


@dataclass
class AskResult:
    ok: bool
    question: str
    trace_id: str
    org_id: int

    sql_raw: str = ""
    sql_final: str = ""
    reasoning: str = ""
    rules_fired: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    as_of: str = ""
    # EXPLAIN 估算的扫描行数（R-11 的判定依据）。原来只进审计不出接口，
    # 界面因此说不出"返回 3 行是从多少行里筛出来的" —— 而这正是判断
    # 一条查询贵不贵、结果可不可信的关键一维
    explain_rows: int | None = None

    rejected_by: str | None = None
    error: str = ""
    hint: str = ""
    #: 结果范围被收窄过（原查询被 R-11 拦下，重试时模型自行加了过滤条件）。
    #: 不出接口就等于没修：这类答案在页面上与全量结果毫无区别。
    scope_narrowed: bool = False
    #: 收窄前那条被拦下的 SQL 与它的预估扫描量，用于向用户说明差在哪。
    scope_note: str = ""
    #: 护栏放行但需提醒的事项（R-24：相对时间问题配了写死的日期）。
    guard_notes: list[str] = field(default_factory=list)
    #: 空结果 / 全零结果的说明。空结果与"真的是 0"在页面上长得一模一样，
    #: 不写这一句，"昨天没有数据"就会被读成"昨天一单都没有"。
    empty_note: str = ""

    tables_hit: list[str] = field(default_factory=list)
    metrics_hit: list[str] = field(default_factory=list)
    #: 召回是盲选：给模型的表不是按相关度选出来的，答案可能答非所问。
    #: 出接口而不只进审计 —— 事后能查出来，救不了正在看这个数字的人。
    recall_blind: bool = False
    recall_note: str = ""
    #: 召回降级过。出接口的理由与 recall_blind 一字不差：事后能查出来，
    #: 救不了正在看这个数字的人。
    recall_degraded: bool = False
    #: 脱敏判定退化过（SQL 解析不出，整行按敏感处理）。
    mask_degraded: bool = False
    #: 本次实际脱敏的列。
    masked_columns: list[str] = field(default_factory=list)
    #: 问句是纯指代追问。
    anaphoric: bool = False
    #: 推理里的猜测措辞。非空 = 模型自认不确定，可信度必须跟着降。
    hedge_terms: list[str] = field(default_factory=list)
    #: 从推理里抹掉的假陈述。留档，让"我们改了模型的话"这件事本身可审计。
    scrubbed_claims: list[str] = field(default_factory=list)
    #: 用到的缓存/派生计数列。这类列与真实计数会漂移，出现即降可信度。
    derived_columns: list[str] = field(default_factory=list)
    #: 结论里追溯不到任何查询返回值的大额数字（BUG-A5）。非空 = 这些数不是从
    #: 库里来的。出接口而不只进审计：事后查得出来，救不了正在看这个数字的人。
    ungrounded_numbers: list[str] = field(default_factory=list)
    #: 本次口径声明，必出。
    caliber: str = ""
    attempts: int = 1
    step_count: int = 1
    multi_step: bool = False
    sub_steps: list[dict[str, Any]] = field(default_factory=list)
    converged_early: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    # 检查点线程。普通提问 == trace_id；续跑时保持原任务的线程不变，
    # 而 trace_id 每次执行新开 —— 审计里由此能看出"这是第 2 次执行"。
    thread_id: str = ""
    elapsed_ms: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cost_cny: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["rows"] = [[jsonable(v) for v in r] for r in self.rows]
        return d


def jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, Decimal):
        return _decimal_str(v)
    return str(v)


def _decimal_str(v: Decimal) -> str:
    """Decimal 转可读文本。

    PostgreSQL 的除法会返回高标度 numeric，而 ``str(Decimal)`` 对这类值走科学计数法：
    比率为 0 时显示成 ``0E-20``，看的人根本认不出这是 0。统一改成定点写法，并去掉
    标度带来的无意义末尾零。
    """
    if not v.is_finite():          # NaN / Infinity 保持原样
        return str(v)
    # normalize: 0E-20 → 0、1.500 → 1.5；但整数会变成 1E+3，再用定点格式化修回来
    return format(v.normalize(), "f")


# --------------------------------------------------------------------------
@dataclass
class _LlmSpan:
    """模型这一步**最终那次尝试**的落账口径。

    只描述最终那次，不是全部尝试之和 —— 之和已经由失败的那几条 span 各自
    记着了，成功那条再记一遍就是把返工的账算两遍。
    """

    status: str = "ok"
    model: str = ""
    ms: int | None = None       # None ＝ 没有可信的单次耗时，退回按节点起点算
    attempt: int = 0
    attempts_total: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cached_in: int = 0
    cost_cny: float = 0.0


def _llm_spans(d: Deps, step: str, usage: Any = None) -> _LlmSpan:
    """把这一步模型的**每次尝试**落成独立 span，返回最终那次的落账口径。

    失败的尝试在这里就地落条，各带自己的错误码、处置与真实烧掉的 token。
    成功那次不在这里落 —— 它还要带业务 note（"生成 1 条 SELECT"、"判定单步
    可答"），只有调用方知道该写什么。

    **每个模型调用点都要调一次，异常分支也不例外。** 不调的话，这一步失败的
    尝试会顺延到下一个节点被取走，落成挂在别人名下的 span —— 那比不记还坏。

    usage 是调用方拿到的合计用量，只在流水为空时兜底（理论上不该发生，
    但宁可退回旧口径，也不要在成功的链路上把 token 记成 0）。
    """
    # 记流水是 LlmClient 的**可选能力**，不进 generate_sql/structured 那份
    # 核心契约：不记流水的实现（测试替身、将来别的模型客户端）就按"只跑了
    # 一次"处理，退回改造前那种一步一条 span 的口径 —— 那仍然是真的，只是
    # 少了返工的细节。要求每个替身都实现它，换来的只是一堆为观测而写的空方法。
    take = getattr(d.llm, "take_attempts", None)
    attempts = take() if callable(take) else []
    total = len(attempts)
    # 只跑一次就成的步骤不写 attempt/attempts_total —— 每条审计凭空多两个
    # 键，乘上几十万条不是小事，而"1/1"本身不含信息。
    n_total = total if total > 1 else 0
    # 没有流水就**不填 model**：这一步到底是谁应答的，此时并不知道。
    # 从配置里的主模型名顶上去正是这次要修的那个 bug —— 切了备选照样记主模型。
    # 空着，_audit_of 会退回旧口径，那至少是"没记"而不是"记错"。
    final = _LlmSpan(attempts_total=n_total)
    if usage is not None:
        final.tok_in, final.tok_out = usage.input_tokens, usage.output_tokens
        final.cached_in, final.cost_cny = usage.cached_input_tokens, usage.cost_cny
    for i, a in enumerate(attempts, 1):
        if a.status == "ok":
            final = _LlmSpan(
                # 被重试或备选救回来的产出**不是 ok**。这一条正是页面上
                # "看不出降级"的那半张脸：一次就成与救回来一次，原来在
                # 状态列上是同一个字。
                status="fallback" if total > 1 else "ok",
                model=a.model, ms=a.ms,
                attempt=i if total > 1 else 0, attempts_total=n_total,
                tok_in=a.usage.input_tokens, tok_out=a.usage.output_tokens,
                cached_in=a.usage.cached_input_tokens, cost_cny=a.usage.cost_cny,
            )
            continue
        # note 写**我们自己的话**，厂商原文进 error_message —— 见 trace.py
        # 那两个字段的注释：/api/trace 免登录可读，不能让 4xx 回显把提示词
        # 里的表结构与用户问题捎出去。
        d.tracer.add(
            step, 0.0, "模型调用失败，未产出", status="failed", ms=a.ms,
            attempt=i if total > 1 else 0, attempts_total=n_total,
            model=a.model, error_code=a.error_code,
            error_message=a.error_message, disposition=a.disposition,
            tok_in=a.usage.input_tokens, tok_out=a.usage.output_tokens,
            cached_in=a.usage.cached_input_tokens, cost_cny=a.usage.cost_cny,
        )
    return final


def _sp_kw(sp: _LlmSpan, status: str = "") -> dict[str, Any]:
    """摊成 tracer.add 的关键字参数。status 非空时按调用方的判定覆盖 ——
    业务上判失败（如"不足以作答"）与模型调用本身成没成，是两件事。"""
    kw: dict[str, Any] = {
        "ms": sp.ms, "status": sp.status, "model": sp.model,
        "attempt": sp.attempt, "attempts_total": sp.attempts_total,
        "tok_in": sp.tok_in, "tok_out": sp.tok_out,
        "cached_in": sp.cached_in, "cost_cny": sp.cost_cny,
    }
    if status:
        kw["status"] = status
    return kw


def _upstream_degraded(d: Deps) -> bool:
    """本次链路在这一步之前是否已经失败或降级过。

    给"返回 0 行"用：零行本身是执行成功，但如果 Schema 召回回落过、模型
    是被备选救回来的，这个 0 就不能当成"确实没有数据"来读。
    """
    return any(st.status in ("failed", "degraded", "fallback") for st in d.tracer.steps)


# 节点
# --------------------------------------------------------------------------

def _n_clarify(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    """纯指代追问在这里就停住 —— 不召回、不生成、不花一分钱。

    放在链路最前面而不是让模型自己判：模型判过了，判得不稳。2026-09-10 的
    1030 次跑测里，「那前三名呢」被答成了 eval_experiments 的 top1 命中数前三，
    reasoning 还写着"沿用上一轮口径"；而同一批里「第二名呢」它又老老实实说了
    "缺少上一轮上下文"。同一类问题两种行为，说明这件事不能交给它自觉。

    判定是确定性的（见 clarify.is_anaphoric），且刻意收得很紧 —— 误判一条正常
    问题的代价比漏判一条追问更大。
    """
    d = _deps(config)
    # 多步链路的子步骤自带【本步目标】，那才是真的"上一步"，不适用这条判定。
    if state.get("goal") or state.get("carry"):
        return {}
    t = d.tracer.start()
    # 发起人已经把缺的那半句补上了，就不该再拦一次。
    #
    # **这是这个节点的出口。** 在此之前它只有一条出路：判定成立就终止链路，
    # 让人"换个问法重新发起"—— 而换个问法等于换一条线程，原来那条永远停在
    # 等待补充。判定本身没错（1030 次跑测证明模型自己判不稳），错在判完之后
    # 没有回来的路。补充**不放宽判定**：它不去猜问句里的指代指向谁，只是把
    # 人给的那句话原样往下传，由 plan/generate 去用。
    extra = str(state.get("clarification") or "").strip()
    if extra:
        d.tracer.add("clarify", t, f"发起人已补充条件：{extra[:60]}")
        return {"clarification": extra}
    v = clarify.is_anaphoric(state["question"], d.cfg)
    if not v.anaphoric:
        d.tracer.add("clarify", t, "问句主体明确")
        return {}
    d.tracer.add("clarify", t, f"需要澄清：{v.reason}", status="blocked")
    return {
        "error": f"这个问题缺少查询对象：{v.reason}。",
        "error_hint": v.ask,
        "rejected_by": "NEED_CONTEXT",
        "anaphoric": True,
    }


def _n_retrieve(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    # 召回要连**发起人补充的条件**一起看，不能只看原问句。
    #
    # 「等待补充」的原问题往往很泛（"帮我看看数据"），只按它召回，命中的是一堆
    # 不相干的表；用户补充「查 documents 表」之后，如果召回仍只读原问句，
    # documents 永远进不了 schema_prompt，generate 只能如实说"给定表里没有它"
    # —— 界面却明明写着「或直接写出表名」。用 _asked 把补充并进召回查询，
    # 写出的表名才真的能被捞回来。首轮没有补充时 _asked == question，行为不变。
    r = schema_rag.recall(_asked(state), d.cfg)
    note = f"命中 {len(r.tables)} 张表（白名单共 {len(d.cfg.tables)} 张）"
    if r.metrics:
        note += f"；命中口径 {'、'.join(m.name for m in r.metrics)}"
    if r.truncated:
        note += f"；因 token 预算裁掉 {'、'.join(r.truncated)}"
    if r.note:
        note += f"；{r.note}"
    # 回落是一次**失败 + 一次降级产出**，不是一次成功。原来两件事都被压进
    # 上面那句 note 的尾巴里，状态列照记 ok —— 配置声明 vector、实际每次都在
    # 跑 keyword，线上这么跑了两天没人看得出来（schema_rag 模块开头那段）。
    # embedding 的账记在这一步：它就是这一步花的钱。model 记嵌入模型名 ——
    # 一条链路可能同时用了生成模型与嵌入模型，按记录级那一个 model 字段
    # 分摊，嵌入这笔就永远挂在生成模型头上。
    embed_kw = {"tok_in": r.embed_tokens, "cost_cny": r.embed_cost,
                "model": r.embed_model} if r.embed_tokens or r.embed_model else {}
    if r.degraded_from:
        d.tracer.add("schema_recall", t,
                     f"{r.degraded_from} 召回不可用", status="failed",
                     ms=r.degrade_ms, error_code=r.degrade_code,
                     error_message=r.degrade_error,
                     # model 那一列只放模型名。召回模式不是模型，塞进去会在
                     # Span 表的「尝试 · xxx」里显示成一个并不存在的模型。
                     disposition=f"回落 {r.mode} 召回（fail-open，不中断链路）")
        # 减掉失败那次自己烧的时间：两条 span 加起来要等于这个节点的真实耗时，
        # 各记一遍全节点就成了凭空多出来的一截。
        node_ms = int((time.perf_counter() - t) * 1000)
        d.tracer.add("schema_recall", t, note, status="degraded",
                     ms=max(0, node_ms - r.degrade_ms), tables=r.table_names,
                     **embed_kw)
    else:
        d.tracer.add("schema_recall", t, note, tables=r.table_names, **embed_kw)
    return {
        "schema_prompt": r.prompt,
        "tables_hit": r.table_names,
        "metrics_hit": [m.name for m in r.metrics],
        "recall_truncated": r.truncated,
        "recall_blind": r.blind,
        "recall_note": r.note,
        "recall_degraded": bool(r.degraded_from),
    }


def _asked(state: AskState) -> str:
    """送进提示词的问题文本 —— 原问题 + 发起人事后补上的条件。

    **绝不改写 state["question"] 本身。** 审计、任务标题、复核队列、指纹比对
    全都读那个字段；就地改掉的话，同一条线程在界面上会变成另一个问题，而
    审批指纹也会对不上（approvals.request 拿原文算指纹，见那里的说明）。
    补充只影响这一次怎么问模型，不影响这条线程是关于什么的。
    """
    q = state["question"]
    extra = str(state.get("clarification") or "").strip()
    return f"{q}\n\n【发起人补充的条件】\n{extra}" if extra else q


def _n_plan(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    """判定单步还是多步；多步时给出本步目标。

    禁用多步（planner.enabled=false）时直接放行，一次模型调用都不花。
    """
    d = _deps(config)
    if not bool(d.cfg.raw.get("planner", {}).get("enabled", False)):
        return {"multi_step": False, "goal": ""}

    t = d.tracer.start()
    step_no = state.get("step_no", 0)
    first = step_no == 0
    try:
        if first:
            plan, usage = d.llm.structured(
                planner.Plan, planner.PLAN_SYSTEM,
                planner.PLAN_USER.format(schema=state["schema_prompt"],
                                         question=_asked(state)))
        else:
            plan, usage = d.llm.structured(
                planner.Plan, planner.REPLAN_SYSTEM,
                planner.REPLAN_USER.format(
                    schema=state["schema_prompt"], question=_asked(state),
                    history=planner.render_history(state.get("steps_done") or []),
                    carry=planner.render_carry(state.get("carry") or {})))
    except QuotaExceeded as e:
        _llm_spans(d, "plan")
        d.tracer.add("plan", t, str(e), status="blocked")
        return {"error": str(e), "error_hint": "明日自动恢复。直查 SQL 不受配额限制。",
                "rejected_by": "QUOTA"}
    except Exception as e:
        # 先落每次尝试，再落这条收尾。主模型与备选都挂了时，页面上要看得到
        # **是谁挂了、各报了什么码**，而不是只有一句拼起来的"规划失败"。
        _llm_spans(d, "plan")
        d.tracer.add("plan", t, f"规划失败：{e}", status="failed")
        return {"error": f"规划失败：{e}",
                "error_hint": "检查网络与密钥；也可关闭 planner.enabled 退回单步。",
                "rejected_by": "LLM"}

    sp = _llm_spans(d, "plan", usage)

    # 重规划时模型给不出目标 —— 按设计不得就此收敛。
    #
    # 设计图 §2.1 里，[8] 结果评估判"不足"后必然回到 [2] 重规划再进 [3] 生成；
    # 这个环的**唯一出口**是 enough=true 或触及 R-16 / R-17 上限，
    # 没有"重规划反悔"这条边。让 plan 推翻 assess 的判定，会出现
    # 两次模型调用互相矛盾：assess 说不够、plan 说够了，白花一轮 token
    # 且用户拿到的是一个 assess 自己都认为不完整的答案。
    #
    # 所以改为：以 assess 的判定为准，用它给出的 next_goal 兜底继续。
    # 不怕转不停 —— R-16 步数上限与 R-17 成本上限就是为此存在的。
    if not first and not plan.goal.strip():
        fallback = (state.get("next_goal") or "").strip()
        if fallback:
            d.tracer.add("plan", t, f"重规划未给出目标，沿用结果评估的判定：{fallback}",
                         **_sp_kw(sp))
            # 不要动 step_no —— 它由 assess 递增，这里再加一次就成了双重递增
            return {"goal": fallback, "multi_step": True, **_spent(state, usage),
                    "attempt": 0, "sql_raw": "", "error": None, "enough": False}
        # 连 assess 都没说清缺什么 —— 此时继续下去也是空转，如实收敛并标注
        d.tracer.add("plan", t, "重规划与结果评估均未给出下一步，收敛作答",
                     **_sp_kw(sp, status="failed"))
        return {"enough": True, "goal": "", **_spent(state, usage),
                "converged_early": "结果评估判定不足，但未能给出下一步目标"}

    if first:
        note = ("判定需多步：" + plan.reason) if plan.multi_step else ("判定单步可答：" + plan.reason)
    else:
        note = f"第 {step_no + 1} 步目标：{plan.goal}"
    d.tracer.add("plan", t, note, **_sp_kw(sp))
    return {"multi_step": bool(plan.multi_step) if first else state.get("multi_step", False),
            "goal": plan.goal or "", "enough": False, **_spent(state, usage)}


# 模型没出 SQL（NO_SQL）时给用户的"下一步"。原来是一句写死的
# "在 config/tables.yaml 中开放更多表" —— 对匿名访客暴露内部配置路径、也没人改得了，
# 而且对"删库/预测/返回密码"这类被拦的请求同样弹这句，把安全拒绝说成"表不够"，误导。
# 改为按模型 reasoning 的意图分型给友好文案，且**任何身份都不出现内部文件路径**。
_WRITE_MARKERS = ("写操作", "只读", "update", "delete", "insert", "改名", "修改", "删除",
                  "插入", "新建", "更新", "truncate", "drop", "alter", "授权", "权限", "写入")
def _today(cfg: Config) -> str:
    """今天是几号 —— 按部署方声明的日界时区算，与审计、配额同一口径。

    喂给模型之前先在这里定死：进程时区是 UTC 而业务在东八区，
    北京时间凌晨到早八点之间"今天"会差一天，而这类错误在答案上
    表现为"数字对不上"，没人会想到是时区。
    """
    tz = day_tz(cfg)
    return (datetime.now(tz) if tz is not None
            else datetime.now().astimezone()).strftime("%Y-%m-%d")


def _no_sql_hint(reasoning: str) -> str:
    low = (reasoning or "").lower()
    if any(m in low for m in _WRITE_MARKERS):
        return "本工具只做只读查询，改动数据、表结构或权限的请求不会执行；换成查询类问题再试。"
    return "换一个更贴近现有数据的问法试试；若确实需要更多数据范围，可联系管理员开放。"


def _n_generate(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    attempt = state.get("attempt", 0)
    t = d.tracer.start()
    try:
        step_ctx = ""
        if state.get("goal"):
            step_ctx = f"\n\n【本步目标】\n{state['goal']}"
            if state.get("carry"):
                step_ctx += ("\n\n【可直接引用的中间结果，按字面量写进 SQL】\n"
                             + planner.render_carry(state["carry"]))
        # 发起人补充的条件走这个槽，而不是拼进 question：question 那个参数
        # 同时是"上次错在哪"的比对基准（last_sql/error 都以它为前提），
        # 混进去会让重试时的提示前后不一致。
        extra = str(state.get("clarification") or "").strip()
        if extra:
            step_ctx += ("\n\n【发起人补充的条件 —— 优先按它确定时间范围、"
                         "口径与统计维度】\n" + extra)
        schema_prompt = state["schema_prompt"]
        # 上一轮是被扫描阈值拦下的 —— 把本库的预聚合汇总表显式补进来。
        # 不补的话模型手上只有明细表，"降低扫描量"就只剩"加个过滤条件"这一条路，
        # 而那条路的终点是一个悄悄收窄了范围的错数（见 _n_dry_run 的 scope_narrowed）。
        if state.get("rejected_by") == "R-11" or state.get("scan_blocked_sql"):
            schema_prompt += schema_rag.summary_hint(d.cfg)
        draft, usage = d.llm.generate_sql(
            question=state["question"],
            schema_prompt=schema_prompt,
            dialect=d.cfg.dialect,
            last_sql=state.get("sql_raw", ""),
            error=state.get("error") or "",
            step=step_ctx,
            today=_today(d.cfg),
        )
    except LlmNotConfigured as e:
        _llm_spans(d, "generate_sql")
        d.tracer.add("generate_sql", t, "未配置模型密钥", status="failed")
        return {"error": str(e), "error_hint": "配置密钥后重试", "rejected_by": "LLM"}
    except QuotaExceeded as e:
        _llm_spans(d, "generate_sql")
        d.tracer.add("generate_sql", t, str(e), status="blocked")
        return {"error": str(e), "error_hint": "明日自动恢复。直查 SQL 不受配额限制。",
                "rejected_by": "QUOTA"}
    except Exception as e:
        # 主模型与备选各自的错误码、耗时、处置先落条，再落这条收尾 ——
        # 只留一句拼接的"模型调用失败"，事后分不出该退避重试还是该换模型。
        _llm_spans(d, "generate_sql")
        d.tracer.add("generate_sql", t, f"模型调用失败：{e}", status="failed")
        return {
            "error": f"模型调用失败：{e}",
            "error_hint": "检查网络与密钥是否有效；也可稍后重试。",
            "rejected_by": "LLM",
        }

    sp = _llm_spans(d, "generate_sql", usage)
    label = "生成 1 条 SELECT" if attempt == 0 else f"第 {attempt + 1} 轮重新生成"
    if sp.status == "fallback":
        # 谁出的活要写在摘要里。状态列那个 FALLBACK 说明"被救回来了"，
        # 但救场的是哪个模型，只有这里说得清。
        label += f"（由 {sp.model} 产出）"
    # **先判有没有 SQL，再落节点**。反过来写的话（这里原来就是反的），模型一条
    # SQL 都没给出来时，节点上照样记着"生成 1 条 SELECT · ok" —— 任务收尾是
    # NO_SQL、节点却显示成功，工具健康度那张表因此永远看不到这类失败，
    # 「主要失败原因」列也就永远是空的。token 与成本两条路都要记：这次调用
    # 真的花了钱，没产出 SQL 不是不计费的理由。
    if not (draft.sql or "").strip():
        why = (draft.reasoning or "模型判断当前表结构无法回答该问题。").strip()
        d.tracer.add("generate_sql", t, f"未生成 SQL：{why}",
                     **_sp_kw(sp, status="failed"))
        return {
            "error": draft.reasoning or "模型判断当前表结构无法回答该问题。",
            "error_hint": _no_sql_hint(draft.reasoning or ""),
            "rejected_by": "NO_SQL",
            "reasoning": draft.reasoning,
            **_spent(state, usage),
        }
    # ---- 输出层：把与事实不符的两类句子从 reasoning 里抹掉 ----
    # 单步查询里不存在"上一轮"，任何"沿用上一轮口径"都是虚构的；护栏做了什么
    # 由 rules_fired 说了算，不由模型转述。抹掉而不是拒答 —— 这些句子挂在一条
    # 可能完全正确的 SQL 上，为一句多余的话丢掉结果不划算。但它们必须消失：
    # 留着就是在替一个没发生的动作背书（2026-09-10 跑测：27 条虚构上一轮、
    # 66 条声称"租户隔离由系统注入"而护栏只注入了 LIMIT）。
    reasoning, scrubbed = clarify.scrub_reasoning(
        draft.reasoning or "",
        multi_step=bool(state.get("goal") or state.get("carry")),
        # 这一步还没过护栏，租户谓词注没注入要到 guard 之后才知道。
        # 保守起见按"没注入"处理：真注入了，guard 那步会把它写进 rewrites，
        # 用户在改写清单上看得到，不靠 reasoning 转述。
        tenant_injected=False,
    )
    hedges = clarify.find_hedges(reasoning)
    if scrubbed:
        d.tracer.add("scrub", t, "已从推理中移除无事实依据的陈述："
                                 + "；".join(scrubbed[:3]), status="blocked")
    if hedges:
        # 猜测措辞不抹 —— 那是模型的真实态度，抹掉反而是掩盖。
        # 它要做的是一路传到可信度上，让分数替用户把这件事说出来。
        d.tracer.add("hedge", t, f"推理含猜测措辞：{'、'.join(hedges[:3])}",
                     status="blocked")
    d.tracer.add("generate_sql", t, label, **_sp_kw(sp))
    return {"sql_raw": draft.sql, "reasoning": reasoning,
            "caliber": (getattr(draft, "caliber", "") or "").strip(),
            "hedge_terms": hedges, "scrubbed_claims": scrubbed,
            "error": None, "rejected_by": None, **_spent(state, usage)}


def _n_guard(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    r = guard.check(state["sql_raw"], d.cfg, org_id=state["org_id"],
                    dialect=d.cfg.dialect, question=state["question"])
    if not r.ok:
        d.tracer.add("guard", t, f"{r.rejected_by} {r.reason}", status="blocked")
        return {"error": r.reason, "rejected_by": r.rejected_by,
                # 被拒的那一版就是本轮生成的这条 —— 必须写进 sql_final。
                # 不写的话它还停留在**上一轮**通过护栏的那条 SQL 上，接口于是
                # 返回"第 3 轮的 SQL + 第 2 轮的报错"：报错说 products 没有
                # order_count，而附着的 SQL 里那一列明明带着 ps. 别名，
                # 照提示改也改不出来（2026-09-09 回归 P-02 实测）。
                "sql_final": state.get("sql_raw", ""),
                # 超范围的拒绝不进反思。路由只读状态，判定在这里定死。
                "out_of_scope": r.out_of_scope,
                "error_hint": ("该对象不在开放范围内。可在接入页查看已开放的表，"
                               "或联系管理员调整白名单。") if r.out_of_scope else ""}

    note = "；".join(r.rewrites) or "无需改写"
    if r.notes:
        # 放行了，但有话要说。挂在 guard 这一步的备注上 —— 判定链路本来就在
        # 页面上展示，比新开一处告警更省事，也不会漏在只看接口的调用方那里。
        note += "｜提醒：" + "；".join(r.notes)
    d.tracer.add("guard", t, note)
    return {
        "sql_final": r.sql, "rules_fired": r.rules_fired, "rewrites": r.rewrites,
        "guard_notes": r.notes,
        "error": None, "rejected_by": None, "out_of_scope": False,
    }


def _n_dry_run(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    try:
        r = d.executor.explain(state["sql_final"])
    except DataSourceError as e:
        # 数据源在链路中途不可用 —— 不是模型的错，别重试
        d.tracer.add("dry_run", t, str(e), status="failed")
        return {"error": str(e), "error_hint": e.hint, "rejected_by": "EXEC"}
    if not r.ok and not d.cfg.scan_waiver:
        d.tracer.add("dry_run", t, r.reason, status="blocked")
        return {
            "error": r.reason,
            "error_hint": "缩小时间范围或加筛选条件，让扫描量降下来。"
                          "确有必要时可申请高成本查询审批。",
            "rejected_by": "R-11",
            # 超阈值不再是终点：server 据此登记一条待审批（P07）
            "needs_approval": True,
            "est_rows": r.est_rows,
            # 记下被拦的那一版。重试若靠"加个过滤条件"跑通，最终结果就不是
            # 用户问的那个范围 —— 到 finalize 时要能说出这件事。
            #
            # **只在真的因为扫描量被拦时才记**。explain 的 ok=False 有两种来源：
            # 扫描量超阈值（est_rows 有值），和执行计划压根生成不出来（类型不匹配
            # 之类的语义错，est_rows 是 None）。后者是一次普通的语义错重试，
            # 重试版本与被拦版本之间的差异是"改对了写法"，不是"收窄了范围" ——
            # 混在一起记，会让一次正常的纠错被判成随手收窄。
            **({"scan_blocked_sql": state.get("sql_final", ""),
                "scan_blocked_rows": r.est_rows} if r.est_rows is not None else {}),
        }
    if not r.ok:
        # 已获批准。如实记下"这一步本该拦下但按审批放行"，
        # 审计里必须看得出这条查询是走审批过来的，否则阈值形同虚设。
        d.tracer.add("dry_run", t, f"{r.reason}（已获审批放行）", status="ok")
        return {"error": None, "rejected_by": None, "explain_rows": r.est_rows,
                "approved_over_threshold": True}
    est = f"预估扫描 {r.est_rows:,} 行" if r.est_rows is not None else "计划无基数估计"
    # 这一版跑通了，但前面有一版是被扫描阈值拦下的 —— 说明模型是靠收窄范围
    # 换来的通过。链路到这里全绿，若不在这里记一笔，后面就再没有地方能记了。
    blocked = state.get("scan_blocked_sql", "")
    if blocked:
        was = state.get("scan_blocked_rows")
        # 收窄成什么范围，决定了这个数还能不能用。
        #
        # 换成预聚合汇总表（换 FROM、不加过滤）是我们希望它走的那条路，结果仍是
        # 全量口径，标一句"范围收窄过"就够。但**随手加一个用户没提过的过滤条件**
        # 是另一回事：跑出来的数根本不是用户问的那个范围。2026-09-10 实测，
        # 问「一共有多少个分块」被收窄成 `WHERE kb_id = 1`，返回一个大大的「0」，
        # 而全量是 1,386,242 —— 旁边那句"此结果只是单个知识库的分块数"的告知
        # 完全没能阻止它被当成答案。
        #
        # 所以这里从"告知"升级为"拒答"：告知解决的是可追溯，解决不了可信。
        arb = guard.arbitrary_narrowing(blocked, state["sql_final"],
                                        state["question"], d.cfg, d.cfg.dialect)
        if arb:
            shown = "、".join(f"`{p}`" for p in arb[:3])
            d.tracer.add("dry_run", t, f"收窄范围是模型自行挑的（{shown}），不作为答案返回",
                         status="blocked")
            return {
                "error": (f"这个问题需要全量扫描，超过了单次查询的扫描阈值。"
                          f"重试时加上的过滤条件（{shown}）是模型自己挑的、"
                          f"不是你问的范围，按这个范围算出来的数不能当答案。"),
                "error_hint": ("换用预聚合的汇总表来问；或缩小到你真正关心的范围"
                               "（写明具体是哪一个、哪段时间）；确需全量可申请高成本查询审批。"),
                "rejected_by": "R-11",
                "needs_approval": True,
                "est_rows": state.get("scan_blocked_rows"),
                # 不再进反思重试：反思只会让它换一个同样随手挑的过滤条件。
                "out_of_scope": True,
            }
        d.tracer.add("dry_run", t, f"{est}（原查询预估 {was:,} 行被 R-11 拦下，"
                                   f"本次是收窄范围后的查询）" if was else est,
                     status="ok")
        return {"error": None, "rejected_by": None, "explain_rows": r.est_rows,
                "scope_narrowed": True}
    d.tracer.add("dry_run", t, est)
    return {"error": None, "rejected_by": None, "explain_rows": r.est_rows}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _all_zero_columns(res: Any) -> list[str]:
    """整列都是 0 的数值列名。全是 NULL 的列不算 —— 那是"没有值"，另一回事。"""
    out: list[str] = []
    for i, name in enumerate(res.columns):
        col = [row[i] for row in res.rows if i < len(row)]
        nums = [v for v in col if _is_number(v)]
        if len(nums) == len(col) and nums and all(v == 0 for v in nums):
            out.append(str(name))
    return out


#: `<某某>.name = '字面量'` 这种**未做规范化**的名称等值。带了 LOWER/REPLACE/TRIM
#: 的不算 —— 那已经是规范化过的写法，零行多半真的是没有。
_NAME_EQ = re.compile(
    r"(?<![\w.(])(?:\w+\.)?(?:name|title|display_name|username|slug|label)\s*=\s*'([^']{1,60})'",
    re.IGNORECASE)


def _name_predicate(sql: str) -> str:
    """SQL 里按名称做精确等值比较的那个字面量。没有就返回空串。"""
    if not sql:
        return ""
    # 出现在同一条谓词里的规范化函数会把它救回来，那种就不提示了
    for m in _NAME_EQ.finditer(sql):
        head = sql[max(0, m.start() - 40):m.start()].lower()
        if any(f in head for f in ("lower(", "upper(", "replace(", "trim(", "regexp")):
            continue
        return m.group(1)
    return ""


def _empty_note(res: Any, sql: str) -> str:
    """把"查不到"与"确实是 0"分开说。

    2026-09-09 回归里这一条连着摔了两次：问「昨天的 GMV」，SQL 写成
    `COALESCE(SUM(gmv), 0)` 配一个没有数据的日期，页面显示 0 ——
    读的人得到的结论是"昨天一单没成"，真相是"昨天的数据还没入库"。
    另一次是枚举值猜错（`status='SIGNED'`，库里是 'DELIVERED'），
    0 行照样以"查询成功"的样子返回。

    两种形态都要认：**0 行**，以及**单行且数值列全是 0/NULL** ——
    后者正是 COALESCE 抹平后的样子，光看 row_count 判断不出来。
    """
    if res.row_count == 0:
        # 零行最常见的真因不是"确实没有数据"，而是**名字没匹配上**：
        # 库里叫「岗位 JD 库」，用户打的是「岗位JD库」，SQL 写成精确等值，
        # 返回零行。2026-09-10 实测这条被页面解释成"筛选条件太严 / 当前租户下
        # 没有这类记录"，用户于是得出"这个库是空的"这个完全错误的结论。
        # 先认这一种，认不出再说时间与枚举。
        named = _name_predicate(sql)
        if named:
            return (f"结果为空。SQL 里按名称精确匹配「{named}」——"
                    f"库里没有**叫这个名字**的记录（名称里的空格、大小写都要完全一致）。"
                    f"先确认名字写对了，再考虑是不是真的没有数据")
        return "结果为空。请确认过滤条件（尤其是时间范围与枚举取值）落在有数据的区间里"
    if not res.rows:
        return ""
    if res.row_count > 1:
        # 多行结果里**整整一列全是 0**。合法的情况有（"异常件数"确实处处为零），
        # 但算错口径的情况更多：实测问「SLA 达成率是哪些客服拖的」，200 行客服
        # 的达标率**全是 0%**，与用户自己给出的 32% 直接冲突，而系统毫无察觉
        # （根因是 CASE 里 AND/OR 没加括号）。一句提醒的代价远小于漏报。
        dead = _all_zero_columns(res)
        if dead:
            return (f"「{'、」「'.join(dead[:2])}」这一列在全部 {res.row_count} 行里都是 0。"
                    "若与你的预期不符，多半是计算口径写错了，请核对 SQL")
        return ""
    cells = list(res.rows[0])
    nums = [v for v in cells if _is_number(v)]
    if not nums or len(nums) != len([v for v in cells if v is not None]):
        return ""                     # 还有非数值列（月份、名称），不是"空结果被抹平"的形状
    if any(v != 0 for v in nums):
        return ""
    # 没有任何过滤条件的全零是**真的全零**（空表），那不需要这句提醒。
    if " where " not in f" {sql.lower()} ":
        return ""
    return ("结果是单行全零。若查询限定了某个时间区间或枚举取值，"
            "请先确认该区间真有数据 ——「查不到」和「值就是 0」在这里长得一样")


def _n_execute(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    try:
        d.executor.set_org(state["org_id"])   # RLS 兜底层读这个上下文
        res = d.executor.run(state["sql_final"],
                             limit_capped="R-09" in (state.get("rules_fired") or []))
    except MaskUnresolved as e:
        # 脱敏判定不出投影来源 —— 库是好的，是我们不敢返回。记成 blocked 而不是
        # failed：工具健康度那张表要能把"数据源坏了"和"我们主动挡下"分开看。
        d.tracer.add("execute", t, str(e), status="blocked")
        return {"error": str(e), "error_hint": e.hint, "rejected_by": "P03",
                # 可重试：让模型把子查询/CTE 摊开，投影来源就解析得出来了
                "exec_retryable": True}
    except DataSourceError as e:
        d.tracer.add("execute", t, str(e), status="failed")
        return {"error": str(e), "error_hint": e.hint, "rejected_by": "EXEC",
                # 超时可重试（模型能缩小查询），连接不可达不可重试
                "exec_retryable": bool(getattr(e, "retryable", False))}
    except Exception as e:
        d.tracer.add("execute", t, f"执行失败：{e}", status="failed")
        return {"error": f"执行失败：{e}", "rejected_by": None}

    note = f"返回 {res.row_count} 行"
    if res.truncated:
        note += "（已按行数上限截断）"
    if res.masked_columns:
        # 脱敏进执行链路的说明里：只把星号显示出来而不说是谁脱的，
        # 看的人第一反应是"库里存的就是这样"。
        note += f"；已脱敏 {len(res.masked_columns)} 列（{'、'.join(res.masked_columns[:5])}）"
    if res.mask_degraded:
        note += "；SQL 解析不出投影来源，本次按整行从严脱敏"
    empty = _empty_note(res, state.get("sql_final", ""))
    if empty:
        note += f"；{empty}"
    # 执行成功但零行 —— 不是 ok，也不是失败。原来它记成 ok，于是"查不到"
    # 与"确实是 0"在状态列上同样看不出区别（note 里那句提醒是自由文本，
    # 统计与筛选都够不着）。
    status = "empty" if res.row_count == 0 else "ok"
    if res.row_count == 0 and _upstream_degraded(d):
        # 上游降级过的零行尤其不能当成"没有数据"来读：召回回落之后，
        # 该查的表可能压根没进模型的视野。
        note += "；本次链路上游存在失败或降级，零行不可直接判定为无数据"
    d.tracer.add("execute", t, note, status=status)
    return {
        "columns": [str(c) for c in res.columns],
        "rows": [[jsonable(v) for v in row] for row in res.rows],
        "row_count": res.row_count, "truncated": res.truncated,
        "as_of": res.as_of, "empty_note": empty,
        "mask_degraded": res.mask_degraded,
        "masked_columns": list(res.masked_columns),
        "error": None, "rejected_by": None,
    }


def _n_assess(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    """本步结果是否足以作答；不足则提取要下传的标识列。

    单步链路直接判定为足够，不花模型调用。
    """
    d = _deps(config)
    t = d.tracer.start()
    step_no = state.get("step_no", 0) + 1
    done = list(state.get("steps_done") or [])
    done.append({
        "index": step_no, "goal": state.get("goal", "") or "（单步）",
        "sql": state.get("sql_final", ""), "row_count": state.get("row_count", 0),
        "columns": list(state.get("columns") or []),
        "preview": planner.preview_rows(state.get("rows") or []),
    })
    base = {"step_no": step_no, "steps_done": done}

    if not state.get("multi_step"):
        d.tracer.add("assess", t, "单步链路，直接收敛")
        return {**base, "enough": True}

    # R-16 / R-17：步数与累计成本上限。触顶后的动作由 on_cap_reached 决定 ——
    # converge（默认）＝基于已完成步骤作答并标注不完整；fail ＝直接失败。
    # 两种都不静默返回部分结果。做成配置是因为这个取舍随场景变：
    # 探索场景宁可拿到半个答案，对账场景宁可什么都不给。
    on_cap = str(d.cfg.raw.get("planner", {}).get("on_cap_reached", "converge")).lower()

    def _cap_hit(why: str) -> dict[str, Any]:
        if on_cap == "fail":
            d.tracer.add("assess", t, f"{why}，按 on_cap_reached=fail 判定失败", status="failed")
            return {**base, "enough": True, "error": f"{why}，未能得出完整结论",
                    "rejected_by": "R-16/R-17", "converged_early": why}
        d.tracer.add("assess", t, f"{why}，收敛作答", status="failed")
        return {**base, "enough": True, "converged_early": why}

    if step_no >= int(state.get("max_steps", 3)):
        return _cap_hit(f"已达步数上限（{state.get('max_steps')} 步）")
    cap = int(state.get("cost_cap_tokens", 0))
    # 读状态而非 Tracer：状态随检查点持久，续跑不清零（中断恢复设计 §4.1）
    used = int(state.get("tok_used", 0))
    if cap and used >= cap:
        return _cap_hit(f"已达累计成本上限（{cap} tokens，已用 {used}）")

    try:
        a, usage = d.llm.structured(
            planner.Assessment, planner.ASSESS_SYSTEM,
            planner.ASSESS_USER.format(
                question=state["question"], goal=state.get("goal", ""),
                sql=" ".join((state.get("sql_final") or "").split()),
                n=state.get("row_count", 0),
                rows=planner.render_carry(
                    {"预览": planner.preview_rows(state.get("rows") or [])})))
    except QuotaExceeded as e:
        # 额度在多步途中用尽：已经跑出来的步骤是有效的，基于它们收敛作答，
        # 并如实标注为什么停在这里 —— 比丢掉已花掉的钱重来一次好。
        _llm_spans(d, "assess")
        d.tracer.add("assess", t, str(e), status="blocked")
        return {**base, "enough": True, "converged_early": str(e)}
    except Exception as e:
        _llm_spans(d, "assess")
        d.tracer.add("assess", t, f"评估失败，按足够处理：{e}", status="failed")
        return {**base, "enough": True}

    sp = _llm_spans(d, "assess", usage)
    if a.enough:
        d.tracer.add("assess", t, f"足以作答 ✓ {a.reason}", **_sp_kw(sp))
        return {**base, "enough": True, "carry": {}, **_spent(state, usage)}

    ok, why = planner.carry_within_limit(a.carry, d.cfg)
    if not ok:
        # R-15：下传规模超限往往说明上一步筛选本身有问题
        d.tracer.add("assess", t, f"{why}，收敛作答（R-15）",
                     **_sp_kw(sp, status="blocked"))
        return {**base, "enough": True, "converged_early": why, **_spent(state, usage)}

    carried = "、".join(f"{k}={v}" for k, v in a.carry.items()) or "无"
    d.tracer.add("assess", t, f"不足以作答 → 重规划（第 {step_no}/{state.get('max_steps')} 步）"
                              f"；下传 {carried}",
                 **_sp_kw(sp, status="failed"))
    return {**base, "enough": False, "carry": a.carry, **_spent(state, usage),
            # 判"还不够"的人最清楚缺什么 —— 目标由 assess 给出，
            # plan 在模型说不出话时据此兜底，而不是推翻 assess 的判定
            "next_goal": (a.next_goal or a.reason or "").strip(),
            "sql_raw": "", "error": None, "attempt": 0}


def _n_reflect(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    n = state.get("attempt", 0) + 1
    d.tracer.add("reflect", t, f"第 {n} 次重试：把真实错误回灌模型重新生成")
    return {"attempt": n}


def _synth_caliber(state: AskState, cfg: Config, derived: list[str]) -> str:
    """口径声明的兜底合成 —— 让它成为**必出字段**而不是"希望模型写"。

    模型自己给的那句最好（它知道自己选了哪个口径），但 2026-09-10 的跑测里
    它给得极不稳定：同一天问「文档数最多的 5 个知识库」声明了口径，问
    「哪个知识库文档量最大」就一句不提，而后者恰恰用了会漂移的缓存计数列。
    靠模型自觉的字段等于没有这个字段，所以这里按链路自己掌握的事实补一句。
    """
    bits: list[str] = []
    if state.get("metrics_hit"):
        bits.append("按业务口径「" + "」「".join(list(state["metrics_hit"])[:2]) + "」")
    tables = list(state.get("tables_hit") or [])
    if tables:
        bits.append("统计对象：" + "、".join(tables[:3]))
    if derived:
        # 这一句是整个字段最要紧的部分：用了缓存计数器必须写在脸上。
        bits.append("数值取自缓存计数列 " + "、".join(derived)
                    + "（由别处维护，与实时统计可能有出入）")
    return "；".join(bits)


def _n_finalize(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    sql = state.get("sql_final") or state.get("sql_raw") or ""
    derived = guard.derived_columns_used(sql, d.cfg, d.cfg.dialect) if sql else []
    caliber = (state.get("caliber") or "").strip()
    synth = _synth_caliber(state, d.cfg, derived)
    if not caliber:
        caliber = synth
    elif derived and all(c not in caliber for c in derived):
        # 模型写了口径但漏掉"用的是缓存列"这件事 —— 补上，不覆盖它原本那句。
        caliber += "；数值取自缓存计数列 " + "、".join(derived)
    if derived:
        d.tracer.add("finalize", t, "本次用到缓存计数列：" + "、".join(derived),
                     status="blocked")
        t = d.tracer.start()
    d.tracer.add("finalize", t, "已附最终 SQL 与判定链路")
    return {"derived_columns": derived, "caliber": caliber}


# --------------------------------------------------------------------------
# 路由 —— 只读状态，不碰运行时依赖
# --------------------------------------------------------------------------

def _route_after_generate(state: AskState) -> Literal["guard", "finalize"]:
    return "finalize" if state.get("rejected_by") in ("LLM", "NO_SQL") else "guard"


def _can_retry(state: AskState) -> bool:
    return state.get("attempt", 0) < state.get("max_retry", 0)


def _route_after_guard(state: AskState) -> Literal["dry_run", "reflect", "finalize"]:
    if not state.get("rejected_by"):
        return "dry_run"
    # 「问题超出范围」的拒绝不进反思：表不会因为再问一次就开放，
    # 重试只有两种结局 —— 白烧两轮 token，或者模型换个能过校验的东西来答。
    # 后者实测发生过：问「chunks 表有多少行」被 R-03 拦下后，重试改成
    # SELECT COUNT(*) FROM documents 并成功执行，返回一个看似合理的数字
    # （trace 8fd3676f7e65，评测里应拒拦截率因此从 100% 掉到 75%）。
    # 那正是 §10.1 列为高危的"沉默的错误"。
    #
    # 代价：模型把表名拼错（document → documents）也不再自动纠正。
    # 接受这个代价 —— 报错里写明了真实原因，而 schema 是全量注入的，
    # 拼错表名远比换个东西答罕见；实测 338 次调用里前者 0 次、后者 1 次。
    if state.get("out_of_scope"):
        return "finalize"
    return "reflect" if _can_retry(state) else "finalize"


def _route_after_dry_run(state: AskState) -> Literal["execute", "reflect", "finalize"]:
    if not state.get("rejected_by"):
        return "execute"
    if state.get("rejected_by") == "EXEC":
        return "finalize"
    # 已经判定"模型自己挑了个范围"的那次拒绝不进反思：再来一轮，它只会换一个
    # 同样是自己挑的过滤条件，而每一轮都在真实烧 token。out_of_scope 的语义
    # 与 _route_after_guard 那处一致 —— 重试改变不了结论的，就别重试。
    if state.get("out_of_scope"):
        return "finalize"
    # 干跑失败两种情形都值得重试：计划生成失败是语义错，
    # 扫描量超限则可以让模型补上筛选条件。次数仍受 R-14 约束。
    return "reflect" if _can_retry(state) else "finalize"


def _route_after_execute(state: AskState) -> Literal["assess", "reflect", "finalize"]:
    if not state.get("error"):
        return "assess"
    # 数据源不可用不是模型的错，重试没有意义；但语句超时是 —— 模型缩小
    # 时间范围或加筛选条件就可能过，与 R-11 干跑超限同理，那条是会重试的。
    # 设计 §5 只写「execute 报错 → reflect」，未区分二者，此处按错误类别细分。
    if state.get("rejected_by") == "EXEC" and not state.get("exec_retryable"):
        return "finalize"
    return "reflect" if _can_retry(state) else "finalize"


def _route_after_assess(state: AskState) -> Literal["plan", "finalize"]:
    return "finalize" if state.get("enough", True) else "plan"


def _build_skeleton() -> StateGraph:
    g = StateGraph(AskState)
    g.add_node("clarify", _n_clarify)
    g.add_node("retrieve", _n_retrieve)
    g.add_node("plan", _n_plan)
    g.add_node("assess", _n_assess)
    g.add_node("generate", _n_generate)
    g.add_node("guard", _n_guard)
    g.add_node("dry_run", _n_dry_run)
    g.add_node("execute", _n_execute)
    g.add_node("reflect", _n_reflect)
    g.add_node("finalize", _n_finalize)

    g.set_entry_point("clarify")
    g.add_conditional_edges(
        "clarify",
        lambda s: "finalize" if s.get("rejected_by") == "NEED_CONTEXT" else "retrieve",
        {"retrieve": "retrieve", "finalize": "finalize"},
    )
    g.add_edge("retrieve", "plan")
    g.add_conditional_edges(
        "plan",
        # 规划失败、或重规划判定该收敛了，都直接结束 —— 不再空转一条 SQL
        lambda s: "finalize" if (s.get("rejected_by") == "LLM" or s.get("enough")) else "generate",
        {"generate": "generate", "finalize": "finalize"},
    )
    g.add_conditional_edges("generate", _route_after_generate,
                            {"guard": "guard", "finalize": "finalize"})
    g.add_conditional_edges("guard", _route_after_guard,
                            {"dry_run": "dry_run", "reflect": "reflect", "finalize": "finalize"})
    g.add_conditional_edges("dry_run", _route_after_dry_run,
                            {"execute": "execute", "reflect": "reflect", "finalize": "finalize"})
    g.add_conditional_edges("execute", _route_after_execute,
                            {"assess": "assess", "reflect": "reflect", "finalize": "finalize"})
    g.add_conditional_edges("assess", _route_after_assess,
                            {"plan": "plan", "finalize": "finalize"})
    g.add_edge("reflect", "generate")
    g.add_edge("finalize", END)
    return g


def build_graph(target: Any = None):
    """编译状态机。

    接检查点的目的是**失败样本可原样复现**（技术设计说明书 §5），
    用于 P3 评测归因，不是在线断点续跑。

    target 三种：None 不接检查点；Path 走 SQLite；Config 由部署决定 ——
    `observability.store: postgres` 时落 PostgreSQL，否则仍是配置里那个
    SQLite 文件。**检查点跟着凭据走同一个开关**，不单独设一个：两者分家
    配置，就会出现"审计在库里、检查点还在某台机器的本地盘上"，而一次
    失败复现需要两者对得上。
    """
    if target is not None and not isinstance(target, Path):
        from . import auditstore

        cfg = target
        if not auditstore.enabled(cfg):
            return build_graph(cfg.checkpoint_db)
        return _build_pg(_build_skeleton())

    checkpoint_db = target
    g = _build_skeleton()
    if checkpoint_db is None:
        return g.compile()
    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
    # 多副本共享同一个检查点库时，这两条是能不能跑的分界线：
    #
    #   WAL          默认的 DELETE 模式下写会阻塞读，两个 Pod 同时跑必然互相踩。
    #                WAL 允许"多读 + 单写"并行，读方完全不受写方影响。
    #                前提是同一台机器的本地盘（这里是 hostPath），网络盘上 WAL 不可用。
    #   busy_timeout 抢不到锁时默认**立刻**报 database is locked。给 5 秒等待窗口，
    #                把"直接失败"变成"稍等一下"—— 检查点写入是毫秒级的，
    #                5 秒足够排队，真等满了那是别的地方出了问题。
    #
    # 单副本下这两条也没有坏处，所以不做条件判断，一律打开。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")   # WAL 下的推荐档位，掉电最多丢最后几次写
    saver = SqliteSaver(conn)
    saver.setup()
    compiled = g.compile(checkpointer=saver)
    compiled._askdb_conn = conn          # 持有连接，避免被 GC 关掉
    return compiled


def _build_pg(g):
    """检查点落 PostgreSQL。

    多副本共享检查点这件事，SQLite 那条路是靠"同一台机器的本地盘 + WAL"
    撑住的；换成 PG 之后这个前提不再需要 —— 副本落在哪个节点都一样，
    而这正是原来那套写法唯一守不住的地方。

    行工厂必须是 dict（saver 的硬要求），所以走 pgstore 的第二个池子。
    setup() 幂等，建表建索引，每个进程跑一次即可。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    from . import pgstore

    saver = PostgresSaver(pgstore.dict_pool())
    saver.setup()
    return g.compile(checkpointer=saver)


_GRAPH = None
_GRAPH_KEY: str | None = None


def replay(trace_id: str, cfg: Config) -> list[dict[str, Any]]:
    """取回某次调用的全部检查点快照，用于失败复现与归因（P3）。"""
    g = build_graph(cfg)
    out: list[dict[str, Any]] = []
    for snap in g.get_state_history({"configurable": {"thread_id": trace_id}}):
        out.append({
            "next": list(snap.next),
            "attempt": snap.values.get("attempt"),
            "sql_raw": snap.values.get("sql_raw", ""),
            "sql_final": snap.values.get("sql_final", ""),
            "rejected_by": snap.values.get("rejected_by"),
            "error": snap.values.get("error"),
        })
    return list(reversed(out))


def _ensure_graph(cfg: Config):
    from . import auditstore

    global _GRAPH, _GRAPH_KEY
    # 换库/换文件都要重编：缓存键必须能区分这两种落点，否则本机切一次
    # store 开关，进程里还拿着上一个 saver
    key = ("pg:" + _pg_key()) if auditstore.enabled(cfg) else str(cfg.checkpoint_db)
    if _GRAPH is None or _GRAPH_KEY != key:
        _GRAPH = build_graph(cfg)
        _GRAPH_KEY = key
    return _GRAPH


def _pg_key() -> str:
    from . import pgstore

    return f"{pgstore.raw_dsn()}|{pgstore.schema()}"


def is_resumable(thread_id: str, cfg: Config) -> bool | None:
    """这条线程现在还能不能续跑。True/False；查不到检查点库时返回 None。

    判定与 resume() 同源（values 有、next 非空），避免"任务中心说能续、
    点下去 404"这种分叉 —— 审计记录只知道上次以 INTERRUPTED 收尾，
    不知道现场到底有没有落盘，也不知道后来是不是已经被续跑跑完了。
    """
    try:
        snap = _ensure_graph(cfg).get_state(
            {"configurable": {"thread_id": thread_id}})
    except Exception:                 # noqa: BLE001
        return None
    return bool(snap.values and snap.next)


def _interrupt_hint(cfg: Config, thread_id: str) -> str:
    """中断提示语必须**先看现场在不在**，再决定要不要承诺可以续跑。

    中断的成因之一就是检查点库本身出问题（写不进去）。那种情况下这条线程
    一条检查点都没有，续跑必然 404 —— 而此前这里无条件写着「判定现场已存入
    检查点，可从断点续跑」，把用户指向一条走不通的路。

    判定口径与 resume() 保持一致（values 有、next 非空才算可续跑），
    否则两边对"可不可以续跑"的看法会分叉。
    """
    state = is_resumable(thread_id, cfg)
    if state is None:
        # 检查点库还没恢复，问不出来。不猜 —— 猜错哪一边都是误导。
        return ("现场是否落盘暂时查不到（检查点库仍不可用）。"
                "稍后到任务中心看这条线程是否标为可续跑。")
    if state:
        return "判定现场已存入检查点，可从断点续跑；续跑另计一次每日配额。"
    return ("本次中断发生在写检查点之前，现场没有落盘，无法续跑 —— "
            "请重新提问。")


#: 续跑被前置校验挡下时写进审计的 rejected_by。**不是终态** ——
#: 检查点还在，条件恢复之后这条线程仍然可以续跑，所以任务状态里它
#: 与 INTERRUPTED 同档（见 audit._thread_status）。
RESUME_BLOCKED = "RESUME_BLOCKED"


@dataclass
class ResumeBlock:
    """续跑前置校验没过的原因。code 进审计，error/hint 给人看。"""

    code: str
    error: str
    hint: str


def precheck_resume(cfg: Config, values: dict[str, Any],
                    ex: Executor) -> ResumeBlock | None:
    """续跑前重新校验权限、Schema 与数据源连接状态，都过才返回 None。

    三项的顺序是有意的：先判不需要 IO 的权限，再判连接（连不上后面两项
    都问不出来），最后比对表结构。

    **权限**看的是**现在**的可见范围：cfg 已经按调用方当前角色收窄过
    （server._scoped），中断期间被收回的表在这里就落不到白名单里。
    **Schema** 比对白名单声明的列与库里实际的列：中断期间改过表结构的话，
    检查点里那条 SQL 引用的列可能已经不存在 —— 直接跑下去要么报错，
    要么更糟：列名被复用成了别的语义，跑通了但答的是另一件事。
    """
    hit = [str(t) for t in (values.get("tables_hit") or [])]

    revoked = [t for t in hit if t not in cfg.tables]
    if revoked:
        return ResumeBlock(
            code=RESUME_BLOCKED,
            error=f"续跑前校验未通过：这条任务用到的表现在不在可见范围内（{'、'.join(revoked)}）",
            hint="权限或白名单在中断期间收窄了。恢复权限后可以再续跑，检查点仍然保留。",
        )

    try:
        ex.connect()
    except DataSourceError as e:
        return ResumeBlock(
            code=RESUME_BLOCKED,
            error=f"续跑前校验未通过：数据源当前连不上（{e}）",
            hint=e.hint or "数据源恢复后可以再续跑，检查点仍然保留。",
        )

    if hit:
        try:
            actual = ex.describe(hit)
        except Exception as e:        # noqa: BLE001 —— 取不到结构就不放行
            return ResumeBlock(
                code=RESUME_BLOCKED,
                error=f"续跑前校验未通过：读不到表结构（{e}）",
                hint="数据源恢复后可以再续跑，检查点仍然保留。",
            )
        drift: list[str] = []
        for name in hit:
            cols = {str(c.get("name")) for c in (actual.get(name) or [])}
            if not cols:
                drift.append(f"{name}（表已不存在）")
                continue
            gone = [c for c in cfg.tables[name].columns if c not in cols]
            if gone:
                drift.append(f"{name}.{'、'.join(gone)}")
        if drift:
            return ResumeBlock(
                code=RESUME_BLOCKED,
                error=f"续跑前校验未通过：表结构在中断期间变了（{'；'.join(drift)}）",
                hint="检查点里的执行计划基于旧结构，续跑会答错。请重新提问，"
                     "或把白名单与库对齐后再续跑。",
            )
    return None


def _scope_note(out: dict[str, Any]) -> str:
    """把"这个数是收窄了范围算出来的"写成一句人话。

    只有一句话是不够的 —— 得说清**收窄前是什么、差多少**，否则用户既不知道
    该不该信这个数，也不知道下一步怎么办。
    """
    if not out.get("scope_narrowed"):
        return ""
    was = out.get("scan_blocked_rows")
    scale = f"（原查询预估扫描 {was:,} 行）" if isinstance(was, int) else ""
    return ("原查询因扫描量超过阈值被拦下" + scale +
            "，当前结果来自模型自行收窄范围后的查询，**不是全量**。"
            "请核对 SQL 里的过滤条件；需要全量口径可改用预聚合的汇总表，"
            "或申请高成本查询审批。")


def _answering_model(steps: Any) -> str:
    """这次链路里**真正出活**的那个模型。

    优先取 generate_sql 那条 —— 答案是那条 SQL 查出来的，它由谁生成，
    这次结果就该记在谁头上。**不能笼统取"最后一条模型 span"**：多步链路里
    判定与自检也各是一次模型调用，而备选只在失败时顶上一次，下一次调用会
    回到主模型。于是"生成切了备选、自检又回到主模型"这种链路，按最后一条取
    就又记成主模型 —— 正是这次要修的那个错，绕一圈原样回来。

    没有 generate_sql（规划失败等）才退回最后一条成功的模型 span；
    一条都没有（直查、命中缓存、老记录）返回空串，交给调用方兜底。
    """
    # **只看真正过模型的节点。** schema_recall 现在也带 model（嵌入模型），
    # 不排除的话，生成失败的链路会退到它头上 —— 审计里就记着"这次由
    # text-embedding-v4 应答"，而嵌入模型一句话都没生成过。
    rows = [st for st in (steps or [])
            if st.get("step") in MODEL_STEPS
            and st.get("status") in ("ok", "fallback") and st.get("model")]
    for st in reversed(rows):
        if st.get("step") == "generate_sql":
            return str(st["model"])
    return str(rows[-1]["model"]) if rows else ""


def _audit_of(result: AskResult, cfg: Config, kind: str,
              explain_rows: Any = None) -> dict[str, Any]:
    """审计记录统一在这里成形 —— ask / resume / 中断三条路共用一个形状。"""
    return {
        "trace_id": result.trace_id, "ts": now_iso(), "kind": kind,
        "thread_id": result.thread_id,
        # **实际应答的模型**，不是配置里声明的主模型。原来这里直接写
        # cfg.llm["model"]，主模型超时切备选跑完，审计里照样记着主模型 ——
        # 页面看不出回退只是表象，库里存的本来就是错的，按模型分摊的成本
        # 也跟着记到了没出活的那个头上。
        "model": _answering_model(result.steps) or cfg.llm.get("model"),
        "org_id": result.org_id, "question": result.question,
        # 结果出自谁的可见范围 —— 少了它，同一个问题在不同角色下拿到不同行数，
        # 事后无从解释
        "role": cfg.role or "ANONYMOUS",
        "user": cfg.user or "",
        # 打的哪个库。多源之后少了它，同一条 SQL 在不同数据源上的结果
        # 事后完全对不上账
        "source": cfg.source_id or "builtin",
        "source_name": cfg.source_name or cfg.path,
        "tables_hit": result.tables_hit, "metrics_hit": result.metrics_hit,
        # 召回是不是盲选、脱了哪几列 —— 两者都必须进审计。
        # 事后复盘一条可疑结果时，第一个要回答的问题就是"模型当时看得见
        # 该看的那张表吗"；脱敏同理，不记就无从证明当时到底脱没脱。
        "recall_blind": result.recall_blind,
        "recall_degraded": result.recall_degraded,
        # 范围被收窄过。与 recall_blind 同一类信息：链路全绿、结果却不可全信，
        # 事后复盘一个对不上的数字时，这是第一个要看的字段。
        "scope_narrowed": result.scope_narrowed,
        "masked_columns": result.masked_columns,
        "mask_degraded": result.mask_degraded,
        # 语义侧的三个信号。与 recall_blind / scope_narrowed 同一类：链路全绿、
        # 结果却不可全信。**必须进审计**，否则追踪页那枚可信度角标与工作台右栏
        # 会算出两个分数 —— 同一次查询两页两个答案，可信侧栏就此作废。
        "hedge_terms": result.hedge_terms,
        "derived_columns": result.derived_columns,
        "anaphoric": result.anaphoric,
        # 我们改过模型的话这件事本身也要可审计
        "scrubbed_claims": result.scrubbed_claims,
        "caliber": result.caliber,
        # 结果被 R-13 截断。与上面两条同类：不进审计的话，事后在追踪页上
        # "只看到前 N 行"和"一共就这么多行"长得一模一样。
        "truncated": result.truncated,
        "sql_raw": result.sql_raw, "sql_final": result.sql_final,
        "rules_fired": result.rules_fired, "rejected_by": result.rejected_by,
        "attempts": result.attempts, "explain_rows": explain_rows,
        "step_count": result.step_count, "multi_step": result.multi_step,
        "converged_early": result.converged_early,
        "rows_returned": result.row_count,
        # 最终结果：答案文本 + 结果列 + **已脱敏**结果行前 N 行。用于登录态的
        # /api/result（追踪详情与任务中心展示"最终结果"）。行来自 result.rows，
        # 已经过 executor 脱敏；这里只截前 N 行、并 jsonable 化（Decimal/时间转字符串）
        # 免得写审计时 json 序列化失败。SQL 全文不进这里 —— 它仍走 replay 那道门。
        "answer": result.reasoning,
        "columns": list(result.columns),
        "rows_preview": [[jsonable(v) for v in r] for r in (result.rows or [])[:RESULT_PREVIEW_ROWS]],
        "elapsed_ms": result.elapsed_ms,
        "tok_in": result.tok_in, "tok_out": result.tok_out,
        "cost_cny": result.cost_cny, "steps": result.steps,
    }


def _execute(cfg: Config, *, question: str, org: int, trace_id: str,
             thread_id: str, kind: str,
             executor: Executor | None, llm: LlmClient | None,
             init: AskState | None) -> AskResult:
    """一次图执行的公共壳：配额快速失败、中断兜底、结果打包、审计双写。

    init 为 None 即恢复语义 —— LangGraph 从该线程最后一个完成的检查点
    继续（恢复粒度是节点：断点所在节点整个重跑，多花的模型调用由
    状态化后的 R-17 兜住）。
    """
    tracer = Tracer()

    # 每日配额快速失败（真正的扣减在 LlmClient 原子预扣）。
    # 续跑同样要过这一关 —— 中断一次、恢复一次是两次真实的模型消费，
    # 不计入就等于开了一条绕过配额的路径（中断恢复设计 §7.1）。
    dq = build_quota(cfg)
    over, used = dq.exhausted()
    if over:
        tracer.add("quota", tracer.start(), f"当日已用 {used}/{dq.limit}", status="blocked")
        result = AskResult(
            ok=False, question=question, trace_id=trace_id, org_id=org,
            thread_id=thread_id, rejected_by="QUOTA",
            error=f"已达当日模型调用上限（{used}/{dq.limit}）",
            hint="明日自动恢复；也可调高配置中的 observability.daily_quota。",
            steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        )
        write_audit(cfg, _audit_of(result, cfg, kind))
        return result

    own_exec = executor is None
    ex = executor or Executor(cfg)
    deps = Deps(cfg=cfg, llm=llm or LlmClient(cfg), executor=ex, tracer=tracer)

    # **发起记录先落盘，再进图。**
    #
    # 收尾记录只在图跑完（或异常被兜住）之后才写。进程在中途被杀时，检查点
    # 已经存了现场，审计却一条都没有 —— 而任务中心完全由审计构建，于是这条
    # 线程从系统里彻底消失：列不出来，凭 thread_id 也续不了（数据源只记在
    # 审计里，读不到就退回内置源）。2026-09-07 实测过一次，补上这条记录之后
    # 同一个线程立刻恢复成 interrupted / resumable 并真的续上了。
    #
    # 它带的是"这条线程存在、归谁、打哪个库、问的什么"，不带结果与成本；
    # read_records 默认把它滤掉，只有任务中心显式要。收尾记录与它共用
    # trace_id，一到就把它顶掉（见 audit.tasks）。
    write_audit(cfg, {
        "trace_id": trace_id, "ts": now_iso(), "kind": kind,
        "phase": PHASE_STARTED,
        "thread_id": thread_id,
        "org_id": org, "question": question,
        "role": cfg.role or "ANONYMOUS", "user": cfg.user or "",
        "source": cfg.source_id or "builtin",
        "source_name": cfg.source_name or cfg.path,
        "rejected_by": None, "steps": [],
    })

    interrupted: Exception | None = None
    out: dict[str, Any] = {}
    try:
        out = _GRAPH.invoke(
            init,
            {
                "recursion_limit": 40,
                "configurable": {"thread_id": thread_id, "deps": deps},
                # LangSmith 环境启用时 run 树以 metadata.trace_id 与本地审计
                # 互相定位；Langfuse 不走 LangChain 集成（见 observe.py），
                # 在审计落盘后按同一条记录上报。
                "run_name": f"askdb.{kind}",
                "metadata": {"trace_id": trace_id, "org_id": org, "kind": kind},
            },
        )
    except Exception as e:            # noqa: BLE001 —— 中断兜底，见下
        # 逃出图的异常（进程级故障、递归上限、检查点库损坏等）说明
        # 执行停在了某个节点边界 —— 检查点已经存了现场。此前这里直接
        # 向上抛成 500，客户端拿不到 trace_id，"可续跑"就无从谈起。
        # 兜住它：留痕、给出续跑入口，但不吞 KeyboardInterrupt/SystemExit。
        interrupted = e
    finally:
        if own_exec:
            ex.close()

    tok_in, tok_out = tracer.tok_in, tracer.tok_out
    # 金额取各步之和：每一步在调用当刻已按**实际应答的模型**、实测缓存命中量
    # 和当刻计费时段结算过了。这里不能再拿 tok_in/tok_out 乘一个单价重算 ——
    # 那会把兜底切走的调用按主模型价记，也会抹掉缓存折扣。
    spent_cny = tracer.cost_cny
    if interrupted is not None:
        tracer.add("interrupted", tracer.start(),
                   f"执行在中断点停止：{interrupted}", status="failed")
        result = AskResult(
            ok=False, question=question, trace_id=trace_id, org_id=org,
            thread_id=thread_id, rejected_by="INTERRUPTED",
            error=f"任务在执行中中断：{interrupted}",
            hint=_interrupt_hint(cfg, thread_id),
            steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
            tok_in=tok_in, tok_out=tok_out,
            cost_cny=spent_cny,
        )
        rec = _audit_of(result, cfg, kind)
        write_audit(cfg, rec)
        observe.report(rec)
        return result

    result = AskResult(
        ok=not out.get("error") and bool(out.get("sql_final")),
        question=question, trace_id=trace_id, org_id=org, thread_id=thread_id,
        sql_raw=out.get("sql_raw", ""), sql_final=out.get("sql_final", ""),
        reasoning=out.get("reasoning", ""),
        rules_fired=list(out.get("rules_fired") or []),
        rewrites=list(out.get("rewrites") or []),
        columns=out.get("columns", []), rows=out.get("rows", []),
        row_count=out.get("row_count", 0), truncated=out.get("truncated", False),
        as_of=out.get("as_of", ""), explain_rows=out.get("explain_rows"),
        rejected_by=out.get("rejected_by"), error=out.get("error") or "",
        hint=out.get("error_hint", ""),
        scope_narrowed=bool(out.get("scope_narrowed", False)),
        scope_note=_scope_note(out),
        guard_notes=list(out.get("guard_notes") or []),
        empty_note=str(out.get("empty_note", "") or ""),
        tables_hit=out.get("tables_hit", []), metrics_hit=out.get("metrics_hit", []),
        recall_blind=bool(out.get("recall_blind", False)),
        recall_note=str(out.get("recall_note", "") or ""),
        recall_degraded=bool(out.get("recall_degraded", False)),
        mask_degraded=bool(out.get("mask_degraded", False)),
        masked_columns=out.get("masked_columns", []),
        anaphoric=bool(out.get("anaphoric", False)),
        hedge_terms=list(out.get("hedge_terms") or []),
        scrubbed_claims=list(out.get("scrubbed_claims") or []),
        derived_columns=list(out.get("derived_columns") or []),
        caliber=str(out.get("caliber", "") or ""),
        attempts=out.get("attempt", 0) + 1,
        step_count=max(out.get("step_no", 1), 1),
        multi_step=bool(out.get("multi_step", False)),
        sub_steps=list(out.get("steps_done") or []),
        converged_early=out.get("converged_early", ""),
        steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        tok_in=tok_in, tok_out=tok_out,
        cost_cny=spent_cny,
    )
    rec = _audit_of(result, cfg, kind, explain_rows=out.get("explain_rows"))
    write_audit(cfg, rec)
    observe.report(rec)          # 观测双写：同一条记录，异步旁路
    return result


def ask(
    question: str,
    cfg: Config,
    org_id: int | None = None,
    executor: Executor | None = None,
    llm: LlmClient | None = None,
) -> AskResult:
    """跑一次完整链路。executor / llm 可注入，便于测试与复用连接。"""
    _ensure_graph(cfg)
    org = cfg.default_org if org_id is None else org_id
    trace_id = uuid.uuid4().hex[:12]

    pl = cfg.raw.get("planner", {}) or {}
    init: AskState = {
        "question": question, "org_id": org, "trace_id": trace_id,
        "attempt": 0, "max_retry": cfg.max_retry, "out_of_scope": False,
        "exec_retryable": False, "next_goal": "",
        # 多步相关的上限进状态而非从 cfg 现取 —— 路由只读状态，
        # 检查点回放时也就能还原出当时真实的约束
        "step_no": 0, "steps_done": [], "carry": {}, "multi_step": False,
        "max_steps": int(pl.get("max_steps", 3)),            # R-16
        "cost_cap_tokens": int(pl.get("cost_cap_tokens", 0)),  # R-17
        "tok_used": 0,                                         # R-17 持久计数
    }
    return _execute(cfg, question=question, org=org, trace_id=trace_id,
                    thread_id=trace_id, kind="ask",
                    executor=executor, llm=llm, init=init)


def resume(
    thread_id: str,
    cfg: Config,
    executor: Executor | None = None,
    llm: LlmClient | None = None,
    *,
    clarification: str = "",
    question: str = "",
    org_id: int | None = None,
) -> AskResult | None:
    """把一条停下来的线程继续往前推（中断恢复设计 V1.1 + 2026-09-11 扩展）。

    **两条路，判据是现场还在不在检查点里：**

    1. ``snap.next`` 非空 —— 真正的中断（进程被杀、递归超限）。从最后一个
       完成的检查点接着跑，节点粒度恢复，已完成的节点不重跑。这是原有语义。
    2. 没有活检查点，但调用方给了 ``question`` —— 这条线程是**正常收尾**的，
       只是收在了"等待补充"上（NO_SQL / NEED_CONTEXT）。重跑整条链路，
       带上补充的条件。

    第 2 条是 2026-09-11 加的，加它是因为「等待补充」此前根本没有出口：
    clarify 与 generate 判完信息不足就终止，界面只能说"换个问法重新发起"，
    而换个问法就是开一条新线程 —— 原来那条永远停在等待补充，线上积压 294 条。
    现在补充回到**同一条线程**：审计里看得出这是第 2 次执行，任务态跟着变。

    两条路的共同点，也是不能动的部分：
    - 只接受调用方自己持有的 thread_id；不存在返回 None，由接口层与"不存在"
      同样处理 —— 不提供任何枚举入口（§4.2）；
    - 检查点线程保持不变，审计写**新的 trace_id**，两条经 thread_id 关联；
    - 另计一次每日配额；补充一次就是一次真实的模型消费，不能白送。
    """
    g = _ensure_graph(cfg)
    snap = g.get_state({"configurable": {"thread_id": thread_id}})
    extra = (clarification or "").strip()
    if not snap.values or not snap.next:
        # 没有活现场。**必须同时有补充条件和问题原文**才走重跑那条路。
        #
        # 缺补充就返回 None（接口层照旧 404）—— 这是有意的，不是漏判：
        # 没有活检查点意味着这条线程已经正常收尾了，不带任何新信息再跑一遍，
        # 拿到的必然还是同一个"信息不足"，只是白花一次配额。原有语义
        # 「没有断点 → 404」也因此一行没变，现存用例照旧成立。
        if not extra or not question.strip():
            return None
        return _rerun_with_clarification(
            cfg, thread_id=thread_id, question=question.strip(),
            clarification=extra, executor=executor, llm=llm,
            org=cfg.default_org if org_id is None else org_id)
    question = str(snap.values.get("question", ""))
    org = int(snap.values.get("org_id", cfg.default_org))
    trace_id = uuid.uuid4().hex[:12]

    # 补充的条件写回检查点，**在续跑之前**。写进状态而不是当参数传下去：
    # 恢复是从图内部继续的，中间节点拿不到 resume() 的入参，只看得到状态。
    # 写失败不拦续跑 —— 没有补充照样能续，那是原有语义。
    if extra:
        try:
            g.update_state({"configurable": {"thread_id": thread_id}},
                           {"clarification": extra})
        except Exception:             # noqa: BLE001
            pass

    # 恢复前重新校验（§恢复原则 02）。中断与续跑之间隔着任意长的时间，
    # 检查点里存的是**中断那一刻**的前提；不重验就是拿旧前提接着跑，
    # 而已经过了 guard 的那条 SQL 在续跑里不会再过一次 guard。
    own_exec = executor is None
    ex = executor or Executor(cfg)
    try:
        block = precheck_resume(cfg, snap.values, ex)
    finally:
        if own_exec:
            ex.close()
    if block is not None:
        result = AskResult(
            ok=False, question=question, trace_id=trace_id, org_id=org,
            thread_id=thread_id, rejected_by=block.code,
            error=block.error, hint=block.hint,
            steps=[{"step": "resume_precheck", "status": "blocked",
                    "ms": 0, "note": block.error}],
        )
        write_audit(cfg, _audit_of(result, cfg, "resume"))
        return result

    return _execute(cfg, question=question, org=org, trace_id=trace_id,
                    thread_id=thread_id, kind="resume",
                    executor=executor, llm=llm, init=None)


def _rerun_with_clarification(
    cfg: Config, *, thread_id: str, question: str, clarification: str,
    executor: Executor | None, llm: LlmClient | None, org: int,
) -> AskResult:
    """带着补充的条件，在**同一条线程**上把这次提问重跑一遍。

    与 ask() 的唯一差别是 thread_id 沿用、init 里多一条 clarification ——
    刻意不复用 ask()：那个函数的语义是"开一条新线程"，给它加一个
    "其实不开新线程"的开关，会让每个读到它的人都得先搞清楚这次是哪种。

    走的是完整链路（召回 → 规划 → 生成 → 护栏 → 试算 → 执行），一道护栏都
    不少。补充**不是**豁免：人给的是查询条件，不是放行票。
    """
    pl = cfg.raw.get("planner", {}) or {}
    trace_id = uuid.uuid4().hex[:12]
    init: AskState = {
        "question": question, "org_id": org, "trace_id": trace_id,
        "attempt": 0, "max_retry": cfg.max_retry, "out_of_scope": False,
        "exec_retryable": False, "next_goal": "",
        "step_no": 0, "steps_done": [], "carry": {}, "multi_step": False,
        "max_steps": int(pl.get("max_steps", 3)),
        "cost_cap_tokens": int(pl.get("cost_cap_tokens", 0)),
        "tok_used": 0,
        "clarification": clarification,
        # **清掉上一轮"信息不足"留在检查点里的失败痕迹。**
        #
        # thread_id 是复用的，_execute 用它 invoke 图时 LangGraph 会把上一次
        # 收尾时的整份状态载回来，再把这里的 init 合并上去 —— 只覆盖出现的键。
        # 上一轮 NO_SQL 留下的 error（那句"信息不足，需用户补充"）不在 init 里，
        # 于是原样留存；generate 节点据 `error` 非空落到 RETRY 模板，而 RETRY
        # 模板里**没有补充条件的位置**（step 只进 USER 模板），用户刚给的澄清
        # 整条被丢掉。结果是补充多少次、写得多具体，generate 拿到的永远是
        # 「原问题 + 上次那句拒绝」，逐字复读同一个"信息不足"——「等待补充」
        # 这条出口形同虚设（线上 waiting_input 积压即源于此）。
        #
        # 补充是一次**带新信息的重跑**，不是"修上一条 SQL"：显式把失败痕迹清零，
        # generate 才会走 USER 模板、把 clarification 喂进去。
        "error": None, "error_hint": "", "rejected_by": None,
        "sql_raw": "", "sql_final": "",
        "scan_blocked_sql": "", "scan_blocked_rows": None, "scope_narrowed": False,
    }
    return _execute(cfg, question=question, org=org,
                    trace_id=trace_id, thread_id=thread_id, kind="clarify",
                    executor=executor, llm=llm, init=init)
