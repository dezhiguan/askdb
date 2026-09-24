"""自主 agent 的 LangGraph 图 —— 把 agent.py 那个 for 循环拆成可落检查点的节点。

为什么拆：此前系统里有两套控制流 —— graph.py 老管道有检查点但能力弱（模型只
吐一条 SQL，列名靠猜）；agent.py 能力强但是纯 Python 循环，没有检查点。于是
「创建任务」「补充条件」「换个问法」这几类最该深挖的请求反被送进老管道，理由是
"任务线要靠检查点续跑"—— 而那个能力生产上近 30 天触发 0 次。拆完只剩一套。

**搬运纪律：逐条搬，不重新设计。** 下面每个节点里的判定都是从真实跑测挣出来的，
语义不该在这次改动里发生任何变化。要改也是搬完之后单独一次。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from .config import Config
from .executor import Executor
from .llm import LlmClient
from .quota import QuotaExceeded
from .trace import Tracer
from . import grounding, planner, tools


class AgentState(TypedDict, total=False):
    """全部字段可序列化 —— 检查点里存的就是这些。

       与 graph.AskState 有意分开：那边是一条固定管道的中间产物（sql_raw/
       rules_fired…），这边是一场决策的现场（history/exec_results/step）。
       """
    question: str
    org_id: int
    trace_id: str
    thread_id: str

    schema_prompt: str
    #: 同一批表的**表头层**（表名 + 一行描述 + 别名，不含列），只给意图预检。
    #: 单列一个字段而不是在 _n_intent 里现渲染：那里拿不到召回挑中的那批
    #: Table 对象，从 tables_hit 反查 cfg.tables 会在运行时源改名/裁表时
    #: 悄悄对不上 —— 与 schema_prompt 同源产出才保证两者是同一批表。
    schema_heads: str
    tables_hit: list[str]
    #: 召回是否"足够完整"：没盲选、没因预算裁表。为真时 schema_prompt 里每张表
    #: 都是全列原值，get_table_schema 对它们一无所加 —— 决策时据此收窄工具暴露面
    #: （见 _hidden_tools）。**必须声明在这里**，理由同下面 action 那条。
    schema_complete: bool
    #: 预检判定的"问的是元数据还是数据"。同样**必须声明在这里**，否则 LangGraph
    #: 按 State 字段过滤节点返回值时会把它静默丢掉，_hidden_tools 永远读到
    #: 默认的 False —— 而那个方向恰好是"看起来正常、只是一直多花一轮"，
    #: 不会有任何报错把它暴露出来。
    metadata_only: bool

    #: 本轮走的是简单问题快路径（见 _n_fast）。**必须声明在这里**，理由同
    #: 上面 metadata_only 那条：LangGraph 按 State 字段过滤节点返回值，
    #: 没声明的键会被静默丢掉，而这一位丢了的症状是"快路径跑完又走了一遍
    #: 完整链路"—— 比改动前还慢，且没有任何报错。
    fast: bool
    #: 快路径产出的两句人话：结果叫什么、口径是什么。_n_finalize 按它们成句。
    fast_label: str
    fast_caliber: str
    #: 越域 / 可答性这两道门已经判过了。快路径回落时据它决定还要不要跑一次
    #: intent —— 判过就直接进 decide，没判过（快路径那次调用本身失败了）才补。
    #: 少了这一位，回落路径会变成 fast + intent + decide × 2，比改动前还多
    #: 一次调用，而症状只是"偶尔更慢"，不会有任何报错。
    prechecked: bool

    history: list[dict[str, Any]] # 回灌进下一轮提示词
    exec_results: list[dict[str, Any]] #每一次执行成功；接地校验要看全部
    last_exec: dict[str, Any] | None
    scan_blocked: dict[str, Any] | None # 最后一次被 R-11拦下的
    last_error: str

    #: 上一轮 decide 的产物：模型挑了哪个工具、参数是什么、是不是要收尾。
    #:
    #: **必须声明在这里。** LangGraph 按 State 的字段过滤节点返回值，没声明的
    #: key 会被静默丢掉 —— 症状是 finish 永远读不到、工具名变成空串，图于是
    #: 一直空转到步数上限，最后判 NO_EVIDENCE。2026-09-12 实测踩过。
    #:
    #: 存普通 dict 而不是 AgentAction 对象：检查点序列化不了 pydantic 模型。
    action: dict[str, Any]

    answer: str
    #: 模型指认的"结论依据的是第几步的执行结果"（AgentAction.answer_step）。
    #: 0 = 没指认，按最后一次执行取。**必须声明在这里**，理由同 action 那条。
    answer_step: int
    converged: str # 为什么提前收敛，空串=正常收尾
    step: int
    step_count: int

    ungrounded: list[str]
    grounding_retried: bool # 只给一次改正机会

    # 早退不 return，写这几个字段再路由到 finalize
    rejected_by: str | None
    error: str
    hint: str

    max_steps: int
    cost_cap: int
    tok_used: int  # ← R-17 必须在 state，不能读 tracer
    #: 接地校验强制返工时一次性追加的预算（见 GROUNDING_RETRY_* 两个常量）。
    #: 不直接改 max_steps / cost_cap：那两个是**用户配的值**，收敛理由里要
    #: 原样报出来，被悄悄改大之后"达步数上限 9"会和配置里的 6 对不上。
    steps_granted: int
    tokens_granted: int


@dataclass
class Deps:
    """随执行走、不进检查点。与 graph.Deps 同一套办法。"""
    cfg: Config
    llm: LlmClient
    executor: Executor
    tracer: Tracer
    ctx: tools.ToolContext
    #: 长任务交接现场（askdb/async_runner.py）。**节点边界据它决定要不要
    #: 提前交接后台** —— "多步""token 过半""大扫描"这三条判据入口拿不到，
    #: 只有跑到这里才确定；而对续跑/自愈/审批代跑这些不经过入口等待的路径，
    #: 这里是唯一的交接点。
    #:
    #: None = 这次执行不参与交接（本机 CLI、评测、单测）。图的语义不变，
    #: 检查它只是"要不要提前告诉等待者别等了"，从不改变执行结果。
    handoff: Any = None

def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def _check_handoff(d: Deps, state: AgentState, *, step: int = 0,
                   tok_used: int = 0, explain_rows: int = 0) -> None:
    """节点边界的交接检查。**只通知，不改变任何执行语义。**

    没有 handoff（CLI / 评测 / 单测）就是空操作；有也只是把等在入口的那个
    请求线程叫醒，图照原样跑下去 —— 交接改的是"结果怎么送达"，
    不是"这次算到哪儿"。任何异常都吞掉：送达方式的优化不该成为查询失败的原因。
    """
    ho = getattr(d, "handoff", None)
    if ho is None:
        return
    try:
        ho.check(step=step or int(state.get("step", 0)),
                 tok_used=tok_used or int(state.get("tok_used", 0)),
                 cost_cap=int(state.get("cost_cap", 0)),
                 explain_rows=explain_rows,
                 scan_threshold=int(d.cfg.raw.get("guard", {}).get("max_scan_rows", 0) or 0))
    except Exception:                 # noqa: BLE001
        pass



# ---------------------------------------------------------------------------
# 模型尝试流水 → span
#
# 采集在 LlmClient（_note_ok / _note_fail 把提示词与厂商原始响应记进流水），
# **消费在这里**。这套消费端原来在 graph.py，2026-09-12 的
# 「agent 迁到 LangGraph，固定管道整条删除」把老管道删掉时一并带走了，于是
# 采集端还在记、却再没有人来取 —— `take_attempts` 在仓库里只剩一个定义，
# 零调用点。界面上的症状是 MODEL 类 span 的「详情」列恒为「—」，而那一列
# 恰恰是"模型到底看到了什么"的唯一出处。
#
# 一起失效的还有三样，都比那一列重：失败的尝试不再各占一条 span（切了备选
# 被救回来的链路，与一次就成的干净链路长得一模一样）、attempt/attempts_total
# 恒 0、status="fallback" 再也不会出现。
#
# 搬回来时逐条照抄 graph.py 原来那版，语义不动。
# ---------------------------------------------------------------------------


@dataclass
class _LlmSpan:
    """一次模型调用**最终**落账的口径。失败的尝试不在这里，它们已就地落条。"""
    status: str = "ok"
    model: str = ""
    ms: int | None = None       # None ＝ 没有可信的单次耗时，退回按节点起点算
    attempt: int = 0
    attempts_total: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cached_in: int = 0
    cost_cny: float = 0.0
    #: 这次调用的提示词全文与厂商原始响应，由 LlmClient 记在 LlmAttempt 上。
    #: 从这里走一遍，所有过模型的节点（intent / decide）**一处接线就全有了**
    #: —— 各自去拼提示词的话，漏一个不会报错，只会让追踪页那一行永远是占位符。
    prompt: str = ""
    raw: str = ""


def _llm_spans(d: Deps, step: str, usage: Any = None) -> _LlmSpan:
    """把这一步模型的**每次尝试**落成独立 span，返回最终那次的落账口径。

    失败的尝试在这里就地落条，各带自己的错误码、处置与真实烧掉的 token。
    成功那次不在这里落 —— 它还要带业务 note（"判定可答"、决策的 thought），
    只有调用方知道该写什么。

    **每个模型调用点都要调一次，异常分支也不例外。** 不调的话，这一步失败的
    尝试会顺延到下一个节点被取走，落成挂在别人名下的 span —— 那比不记还坏。

    usage 是调用方拿到的合计用量，只在流水为空时兜底（理论上不该发生，
    但宁可退回旧口径，也不要在成功的链路上把 token 记成 0）。
    """
    # 记流水是 LlmClient 的**可选能力**，不进 structured 那份核心契约：
    # 不记流水的实现（测试替身、将来别的模型客户端）就按"只跑了一次"处理，
    # 退回一步一条 span 的口径 —— 那仍然是真的，只是少了返工的细节。
    take = getattr(d.llm, "take_attempts", None)
    attempts = take() if callable(take) else []
    total = len(attempts)
    # 只跑一次就成的步骤不写 attempt/attempts_total —— 每条审计凭空多两个键，
    # 乘上几十万条不是小事，而"1/1"本身不含信息。
    n_total = total if total > 1 else 0
    # 没有流水就**不填 model**：这一步到底是谁应答的，此时并不知道。
    # 从配置里的主模型名顶上去会让"切了备选照样记主模型"这个老 bug 复活。
    final = _LlmSpan(attempts_total=n_total)
    if usage is not None:
        final.tok_in = getattr(usage, "input_tokens", 0)
        final.tok_out = getattr(usage, "output_tokens", 0)
        # 缓存命中量与金额按可选取：与上面"记流水是可选能力"同一条口径 ——
        # 兜底分支只在流水为空时才走到（生产的 LlmClient 总是记流水，拿到的
        # 是完整的 LlmUsage），这里宽容的只是简化过的客户端实现。
        final.cached_in = getattr(usage, "cached_input_tokens", 0)
        final.cost_cny = getattr(usage, "cost_cny", 0.0)
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
                prompt=a.prompt, raw=a.raw,
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
            # 失败那次的提示词最该留：它就是"为什么会失败"的现场。
            # raw 在调用直接抛异常时为空，格式失灵时不空 —— 后者正是要看的。
            input=a.prompt, output=a.raw,
        )
    return final


def _sp_kw(sp: _LlmSpan, status: str = "") -> dict[str, Any]:
    """摊成 tracer.add 的关键字参数。status 非空时按调用方的判定覆盖 ——
    业务上判失败与模型调用本身成没成，是两件事。"""
    kw: dict[str, Any] = {
        "ms": sp.ms, "status": sp.status, "model": sp.model,
        "attempt": sp.attempt, "attempts_total": sp.attempts_total,
        "tok_in": sp.tok_in, "tok_out": sp.tok_out,
        "cached_in": sp.cached_in, "cost_cny": sp.cost_cny,
        "input": sp.prompt, "output": sp.raw,
    }
    if status:
        kw["status"] = status
    return kw


# ---------------------------------------------------------------------------
# 节点
#
# 全部从 agent.py 那个 for 循环逐段搬过来，**语义不做任何改动**。
# 唯一的结构性变化：原来 `return _result(...)` 的早退，改成在 state 上写
# rejected_by/error/hint，再由条件边路由到 finalize —— LangGraph 的节点只能
# 返回状态增量，"提前结束"必须表达成一条边。
#
# agent 的 prompt 与小工具函数一律**延迟 import**：agent.py 要 import 本模块，
# 顶层互相 import 会成环。
# ---------------------------------------------------------------------------

def _n_recall(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """grounded 召回：先把可用的表结构摆到模型面前，再让它决策。

    这一步**不是工具调用** —— 模型既选不了也跳不过，所以 span 类型是 RAG
    而不是 TOOL（见 frontend/src/traceSteps.ts 那段说明）。
    """
    from .agent import _brief, _fastpath_mode

    d = _deps(config)
    t = d.tracer.start()
    # backend 递下去，值检索才跑得起来（它要拿提问里的取值去真实数据里探一次）。
    # 复用链路已有的那个只读执行器，不另开连接。
    rec = tools.search_schema(state["question"], d.cfg,
                              getattr(d.executor, "backend", None))
    tables_hit = rec.data.get("tables", [])
    schema_prompt = rec.data.get("prompt", "")
    schema_heads = rec.data.get("prompt_heads", "") or schema_prompt
    # 输出必须是**喂进提示词的表结构全文**：排查"模型为什么没用那张表"时，
    # 召回对了但结构没渲染出某一列，与压根没召回那张表，在"召回 N 张表"
    # 这句 note 上完全一样，只有全文分得开。
    #
    # **embedding 的用量与金额必须一起落。** 这一行原来只传 tables/input/output，
    # 于是 vector 召回那次 embedding 调用在 trace 里既没有 token 也没有金额 ——
    # 生产 trace 4bac5ce7f21b 的 ¥0.012834 就不含它，schema_recall 那一格
    # tok_in=0 / cost=0，看上去像是"这一步不花钱"。tools.search_schema 的返回体
    # 里 embed_tokens / embed_cost_cny 一直是现成的，是这里把它们丢了。
    #
    # 金额很小，但方向是错的：做成本优化的前提是账面完整，而
    # trace.embed_cost_cny 的注释自己就写着"不设默认价，0 元在成本页上是显眼的、
    # 会被人问起来" —— 这里正是那个该被问起来的 0。
    #
    # embedding 只有输入没有输出，记进 tok_in 与模型调用的口径一致；keyword
    # 模式下这三项恒为空/0，如实记 0。
    #
    # **model 必须一起带上嵌入模型名**，否则 audit 那张按模型分的成本表会走到
    # "带金额却没记模型"那条兜底分支，把这笔 embedding 的钱挂到记录级的应答
    # 模型（qwen3.8-flash）头上 —— 账面合得上，归属是错的。带上之后它记在
    # text-embedding-v4 名下，次数与金额同源。
    #
    # 不会污染别处：MODEL_STEPS 不含 schema_recall，所以「模型调用成功率」的
    # 分母不受影响；graph._answering_model 也按 MODEL_STEPS 过滤，不会把
    # "这次由 text-embedding-v4 应答"写进审计（那条注释正是为此写的）。
    d.tracer.add("schema_recall", t, _brief(rec), tables=tables_hit,
                 tok_in=int(rec.data.get("embed_tokens") or 0),
                 cost_cny=float(rec.data.get("embed_cost_cny") or 0.0),
                 model=str(rec.data.get("embed_model") or ""),
                 input=state["question"], output=schema_prompt)
    # 盲选 / 有表被预算裁掉时，提示词里这份就**不是**全部可用的表，
    # 此时 get_table_schema 仍有用武之地（去查一张没被注入的表）。
    complete = bool(schema_prompt) and not rec.data.get("blind") \
        and not rec.data.get("truncated")
    out: dict[str, Any] = {
        "tables_hit": tables_hit, "schema_prompt": schema_prompt,
        "schema_heads": schema_heads, "schema_complete": complete}

    # 简单问题分流。**判定在这里做、结论写进 state**，路由函数只读那一位 ——
    # LangGraph 的路由改不了状态，两处各判一次就会漂（见 _after_recall）。
    #
    # 判据要看召回的结果（完整没完整、命中几张表），所以只能等到这一步；
    # 而它纯代码、不花 token，放在这里不额外增加任何开销。
    mode = _fastpath_mode(d.cfg)
    if mode != "off":
        # 计时器起在判定**之前**。它现在是纯字符串扫描、确实接近 0ms，
        # 但 add(start()) 那种写法记的永远是 0，将来这条判据长出真正的开销时
        # 在链路上看不出来 —— 与 _n_ground 里那段是同一条理由。
        ft = d.tracer.start()
        why = simple_question({**state, **out})
        if not why:
            if mode == "on":
                out["fast"] = True
            else:
                # shadow：判据照跑、结论留痕，但仍走完整链路。事后按这条 span
                # 把命中的那些拉出来，比对完整链路的答案，量"短路会不会答少"。
                # 这是切 on 之前唯一拿得到真实误判形状的办法。
                d.tracer.add("fastpath", ft,
                             "判定可走快路径（影子档，仍走完整链路）",
                             status="degraded")
        elif mode == "on":
            # 为什么这题没走快路径，要能在链路上看见 —— 否则"它有时快有时慢"
            # 在追踪页上是一件没有解释的事。
            d.tracer.add("fastpath", ft, f"不走快路径：{why}", status="degraded")
    return out


#: 快路径**只受理**带这些词的问题 —— 一次聚合或一次取前 N 行。
#:
#: 用白名单而不是黑名单：黑名单的失败方向是"没想到的问法被放进快路径"，
#: 而这里每一次误放行都是一个可能答短的答案。白名单漏掉的那些只是走回
#: 完整链路，代价为零。两个方向不对称，所以宁可漏。
_SIMPLE_HINTS = (
    "多少", "几个", "几条", "几家", "几张", "总数", "总共", "一共", "总量",
    "最大", "最高", "最低", "最小", "最多", "最少", "最新", "最近", "最早",
    "平均", "均值", "列出", "列一下", "查一下", "看一下", "有没有",
)

#: 命中任何一条就**不走**快路径 —— 这些词意味着分组、对比、关联或解释，
#: 一条 SELECT 说不清，或者说得清也该让完整链路去核一遍。
_COMPLEX_HINTS = (
    "各", "每个", "每种", "每家", "每天", "每月", "分别", "分组", "按",
    "对比", "相比", "比较", "同比", "环比", "占比", "比例", "百分",
    "趋势", "变化", "增长", "分布", "排名", "排行", "top", "TOP",
    "关联", "连表", "以及", "并且", "还有", "同时", "另外",
    "为什么", "原因", "分析", "评估", "建议", "是否合理", "健康",
    "和", "与",          # "A 和 B 各多少" —— 两个实体，必然不止一条 SELECT
)

#: 元数据问题一律不走快路径。它们的证据来自 schema 而不是结果行，
#: 快路径的模板成句拿不出数字，而 _n_finalize 的闸 ①（NO_EVIDENCE）
#: 正是按"有没有成功执行过工具"判的 —— 见 _hidden_tools 里那一大段。
#: 这一档交回完整链路，行为与改动前逐字相同。
_META_HINTS = (
    "哪些表", "什么表", "几张表", "表结构", "哪些字段", "什么字段",
    "哪些列", "什么列", "能查什么", "有什么数据", "字段含义", "表名",
)

#: 快路径受理的问题长度上限（字符）。超过这个长度的问法，经验上总带着
#: 附加条件、口径说明或两个以上的诉求 —— 那些正是一条 SELECT 答不全的。
FAST_MAX_QUESTION = 40

#: 快路径认可的结果规模上限。一条 SELECT 查回来上百行，说明它多半是在
#: 列举而不是在回答；模板成句也没法把上百行浓缩成一句话。
#: 超过就回落完整链路，由模型自己归因。
FAST_MAX_ROWS = 20


def simple_question(state: AgentState) -> str:
    """这个问题能不能走快路径。**纯代码判定，不问模型，不花 token。**

    返回空串 = 可以走；非空 = 不走，内容是这一次被挡下的理由（落进 span，
    否则"为什么这题没走快路径"在链路上没有答案）。

    判据全部保守，且每一条的失败方向都朝"回落完整链路"倒 —— 挡错了只是
    慢回改动前，放错了才会答短。
    """
    if state.get("history"):
        # 有历史 = 澄清补充过、或已经跑过一轮。快路径只受理第一轮：
        # 它的提示词里没有【已完成的工具调用与结果】那一段，看不见前情。
        return "非首轮"
    if not state.get("schema_complete"):
        # 盲选或有表被预算裁掉：提示词里那份不是全部可用的表，
        # 一条 SELECT 很可能写在错的表上，而快路径没有第二次机会去纠正。
        return "召回不完整"
    if not state.get("tables_hit"):
        return "召回为空"
    q = str(state.get("question") or "").strip()
    if not q or len(q) > FAST_MAX_QUESTION:
        return f"问题长度 {len(q)} 超过 {FAST_MAX_QUESTION}"
    if any(w in q for w in _META_HINTS):
        return "元数据问题"
    if not any(w in q for w in _SIMPLE_HINTS):
        return "不含单次聚合/列举的问法"
    hit = [w for w in _COMPLEX_HINTS if w in q]
    if hit:
        return f"含复杂信号词 {'/'.join(hit[:3])}"
    return ""


def fast_result_ok(data: dict[str, Any] | None) -> str:
    """快路径拿回的这份结果，够不够直接成句。空串 = 够。

    **不够就回落完整链路，不是报错。** 这是快路径唯一的安全网：判据放行了、
    SQL 也跑通了，但结果的形状说明这题没那么简单（零行、几十行、宽表），
    那就当作没走过快路径，交回 decide 重新来 —— 代价是这一次多花一轮，
    而收益是快路径永远不会把一份说不清的结果硬编成一句话。
    """
    if not data:
        return "无结果"
    rows = list(data.get("rows") or [])
    if not rows:
        # 零行本身可能就是答案（"有没有 X" → 没有），但也可能是 SQL 写错了
        # 表或条件。分不开，交回完整链路 —— 那边有 empty_note 那套说法。
        return "零行"
    if len(rows) > FAST_MAX_ROWS:
        return f"{len(rows)} 行超过 {FAST_MAX_ROWS}"
    if data.get("truncated"):
        return "结果被截断"
    return ""


def _n_fast(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """简单问题快路径：**一次调用同时完成预检与产 SQL**。

    这个节点替换的是 intent + 第一轮 decide 两次往返，不是绕过它们 ——
    越域、不可答、需要多步这三道门逐条还在（agent.FastSql 的前三位，
    判据文字与 INTENT_SYSTEM 同源），只是不再各开一次往返去问。

    走得通时整条链路是：recall → fast → act → finalize，**一次模型调用**。
    走不通时把这一次的预检结论带着回落 decide，完整链路照跑 —— 预检不重跑，
    所以回落路径的调用次数与改动前持平（fast 顶掉了 intent 那一次）。

    SQL 一个字都不额外放行：它照旧经 _n_act 交给 tools.execute_sql，
    guard / 干跑 / 只读 / 脱敏 / R-11 / R-12 全在那条路上，与模型自己
    挑工具发出来的那条 SQL 走的是同一个安全原子。
    """
    from .agent import FAST_SYSTEM, FAST_USER, FastSql, _sys

    d = _deps(config)
    t = d.tracer.start()
    try:
        fast, u = d.llm.structured(
            FastSql, _sys(FAST_SYSTEM, d.cfg),
            FAST_USER.format(schema=state.get("schema_prompt", ""),
                             question=state["question"]))
    except QuotaExceeded as e:
        _llm_spans(d, "fast")
        d.tracer.add("fast", t, str(e), status="blocked")
        return {"rejected_by": "QUOTA", "error": str(e), "hint": "明日自动恢复。"}
    except Exception as e:                        # noqa: BLE001
        # **失败不拒答，回落完整链路。** 与 _n_intent 那条分支刻意不同：
        # 预检失败时链路确实无从继续，而快路径失败时完整链路原封不动还在，
        # 把一次加速尝试的失败升级成整条查询的失败毫无道理。
        _llm_spans(d, "fast")
        d.tracer.add("fast", t, f"快路径失败，回落完整链路：{e}", status="degraded")
        return {"fast": False}
    sp = _llm_spans(d, "fast", u)

    out: dict[str, Any] = {
        "tok_used": state.get("tok_used", 0) + u.input_tokens + u.output_tokens,
        # 快路径只受理数据问题（_META_HINTS 已把元数据挡在外面），所以这一位
        # 恒为 False。显式写出来而不是靠默认值：回落时 _hidden_tools 要读它，
        # 读到的必须是一个**判过的** False，不是"没人填过"的 False。
        "metadata_only": False,
    }
    # 三道门与 _n_intent 逐条对齐：同样的判据、同样的 rejected_by、
    # 同样的两种不可答分开报。这里只是把它们挪进了同一次调用。
    if fast.out_of_scope:
        d.tracer.add("fast", t, fast.reason, **_sp_kw(sp))
        out.update({"rejected_by": "OOS", "reasoning": fast.reason,
                    "error": fast.reason
                             or "该问题涉及的业务实体在当前库中不存在，无法回答。"})
        return out
    if not fast.answerable:
        d.tracer.add("fast", t, fast.reason, **_sp_kw(sp))
        out.update({"rejected_by": "CLARIFY", "reasoning": fast.clarify,
                    "error": fast.clarify or "问题缺少明确的查询对象，请补充。"})
        return out

    # 两道门都放行了 —— 这一位让回落路径知道预检不必重跑。
    out["prechecked"] = True

    sql = (fast.sql or "").strip().rstrip(";").strip()
    if fast.too_complex or not sql:
        why = "模型判定需要完整链路" if fast.too_complex else "未产出 SQL"
        d.tracer.add("fast", t, f"{why}，回落完整链路：{fast.reason}",
                     status="degraded", **_sp_kw(sp))
        out["fast"] = False
        return out

    d.tracer.add("fast", t, f"一条 SELECT 直答：{fast.label or fast.reason}",
                 **_sp_kw(sp))
    # 伪装成一次 decide 的产物交给 _n_act —— 执行路径一行都不重写，
    # 重复动作检测、R-11 换写法、exec_results 累加全部原样复用。
    out.update({
        "fast": True,
        "fast_label": (fast.label or "").strip(),
        "fast_caliber": (fast.caliber or "").strip(),
        "step": state.get("step", 0) + 1,
        "action": {"finish": False, "answer": "",
                   "tool": "execute_sql", "args": {"sql": sql}},
    })
    return out


def _n_intent(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """意图 / 可答性预检。超出这个库的范围就别开始烧 token。"""
    from .agent import (INTENT_SYSTEM, INTENT_USER, IntentCheck, _sys,
                        intent_schema_heads)

    d = _deps(config)
    t = d.tracer.start()
    try:
        intent, u = d.llm.structured(
            IntentCheck, _sys(INTENT_SYSTEM, d.cfg),
            # **喂表头层，不喂列级明细。** 预检要判的是"有没有承载这个实体的
            # 表"，列名是它被明确要求忽略的那类证据（见 INTENT_SYSTEM 第 2 条
            # 与 schema_rag.table_head）。4bac5ce7f21b 上这一段从 5,103 字符
            # 降到约 900。取不到表头层时退回全量 —— 少喂不如多喂。
            INTENT_USER.format(
                schema=(state.get("schema_heads") if intent_schema_heads(d.cfg) else "")
                       or state.get("schema_prompt", ""),
                question=state["question"]))
    except QuotaExceeded as e:
        # 异常分支也要取流水：不取，这一步失败的尝试会顺延到下一个节点被取走，
        # 落成挂在别人名下的 span —— 比不记还坏。
        _llm_spans(d, "intent")
        d.tracer.add("intent", t, str(e), status="blocked")
        return {"rejected_by": "QUOTA", "error": str(e), "hint": "明日自动恢复。"}
    except Exception as e:                        # noqa: BLE001
        _llm_spans(d, "intent")
        d.tracer.add("intent", t, f"预检失败：{e}", status="failed")
        return {"rejected_by": "LLM", "error": f"意图预检失败：{e}",
                "hint": "检查网络与密钥。"}
    sp = _llm_spans(d, "intent", u)
    d.tracer.add("intent", t, intent.reason, **_sp_kw(sp))

    out: dict[str, Any] = {
        "tok_used": state.get("tok_used", 0) + u.input_tokens + u.output_tokens,
        # 供 _hidden_tools 决定第一轮要不要把 search_schema 摆上桌。
        # 这一位**只由预检产出**：循环里没有任何一处比这次调用更清楚用户问的是
        # 元数据还是数据，而预检本来就要跑，多这一个字段约 10 个输出 token。
        "metadata_only": bool(intent.metadata_only)}
    # 两种不可答分开报：越界是"这个库里没有这种东西"（补充再多也没用），
    # 缺主体是"你问得不够具体"（补一句就能跑）。下一步该谁动手完全不同。
    if intent.out_of_scope:
        out.update({"rejected_by": "OOS", "reasoning": intent.reason,
                    "error": intent.reason
                             or "该问题涉及的业务实体在当前库中不存在，无法回答。"})
    elif not intent.answerable:
        out.update({"rejected_by": "CLARIFY", "reasoning": intent.clarify,
                    "error": intent.clarify or "问题缺少明确的查询对象，请补充。"})
    return out


#: 接地校验回灌时塞进 history 的伪工具名（见 _n_ground）。下一次 decide 看到它，
#: 就是在为"结论里有查不到的数"返工 —— 这是"反思重试"唯一可靠的判据。
GROUND_RETRY_TOOL = "(接地校验)"


def _decide_stage(action: Any, history: list[dict[str, Any]],
                  finish: bool | None = None) -> str:
    """这次决策在链路里担的是哪一档活。

    一条 agent 链路上 `decide` 会连着出现五六次，平铺着看不出哪次是在挑工具、
    哪次是看完结果决定再查一轮、哪次是在返工 —— 而"自检了四轮才收敛"正是读
    这条链路时最该一眼看到的事。

    判据全部现成：模型这次的 action，加上 history 最后一项是什么。

    **不复用老管道那几个 step id**（assess / reflect / finalize）。它们在
    2026-09-12 之前指的是固定管道里的固定节点，审计库里还躺着几百条；复用之后
    同一个 id 在时间轴两侧是两件不同的事，"assess 平均耗时"这类统计会把两种
    东西混在一起算。所以另开一个 stage 字段，step 仍然是 decide。
    """
    last = history[-1] if history else None
    last_tool = str((last or {}).get("tool") or "")
    # **返工优先于收敛**：接地校验打回之后模型直接给答案（没再查一次），这一次
    # 既是返工也是收尾。标成"反思重试"，因为"它是被打回来才重写的"在别处一点
    # 痕迹都没有，而"这是最后一步"看位置就知道。
    if last_tool == GROUND_RETRY_TOOL:
        return "reflect"
    # finish 传归一之后的值：模型同时填了 finish 与 tool 时按工具走，这一步
    # 就不是"收敛作答"，标成 converge 会让链路上出现一个收了尾却又继续跑的格子。
    if (getattr(action, "finish", False) if finish is None else finish):
        return "converge"                # 收敛作答
    if last_tool:
        return "assess"                  # 看过上一份结果之后再决定查什么
    return "select"                      # 还没查过任何东西，纯挑工具


def _hidden_tools(state: AgentState) -> frozenset[str]:
    """这一轮决策**不摆上桌**的工具。

    2026-09-14 线上 trace 3f16cd49baec：5 次模型调用里有 2 次是去取提示词里
    已经逐字写着的东西 ——

      · 第 1 轮调 search_schema。而 _n_recall 调的就是同一个函数、同一个问题，
        召回全文早已注入 AGENT_USER 的 {schema}。这一轮换回同一批 12 张表，
        代价是 3,318ms + 4,022 输入 token + 又一次 embedding 计费调用。
      · 第 2 轮调 get_table_schema("payments")。而 schema_rag.table_doc 渲染的
        就是每张表的全部列名/类型/desc/enum，那 10 列早在提示词里。
        代价 2,013ms + 4,104 token。

    是提示词在教它这么干：规则第一条写着"拿不准列名先用 get_table_schema 查
    清楚"、工具规格写着"拿不准列名/口径时先查它" —— 两句都是**预注入 schema
    之前**留下的。措辞已经改掉，但只靠措辞是概率问题；这里再从暴露面上收一道。

    收窄不等于摘掉：见 agent._render_specs 的说明，模型硬要调仍然调得到。

    **search_schema 为什么留在桌上 —— 撤过，撤不掉。** 闸 ①（_n_finalize 里那条
    NO_EVIDENCE）的判据是"模型自己有没有成功调过工具"，而元数据问题（"这个库里
    有哪些表"）唯一能调的就是它。撤掉之后模型只能零工具调用直接作答，闸 ① 一刀切
    拒 —— 把一个今天只是潜伏的误杀变成系统性的。

    那就改闸 ① 的判据、让召回也算"有依据"？试过，被
    tests/test_agent_runtime.py::test_agent_no_evidence_blocks_fabricated_number
    当场拦下，而且它拦得对：退到闸 ② 之后，「大约有 120 万条订单」里的 120
    小于 grounding.MIN_ABS（1000），那一层按自己的标定根本不查它，凭空编的
    订单量就直接放行了。要走通得另起一套"这个数是不是只由 schema 解释得了"的
    判定 —— 那是个该自己单独标定、先影子跑的改动，不是顺手塞进这个补丁的东西。

    所以这里只收 get_table_schema：它与闸 ① 无关（元数据的依据来自 search_schema），
    撤掉零风险。search_schema 那一轮改由提示词劝阻（AGENT_USER 表头）+ 下面
    _n_act 的 ⓪′ 兜底 —— 劝不住时至少不重跑 embedding，并在回灌里点破。

    2026-09-15 补：**search_schema 也收，但只对数据问题收。**

    上面那段留下的缺口，生产 trace 4bac5ce7f21b 又原样重演了一次：第 2 轮决策
    选了 search_schema，⓪′ 把它挡下、返回 reused=true、耗时 0ms —— embedding
    是省下了，可那一轮决策本身（2,940ms + 4,355 输入 / 225 输出 token
    ≈ ¥0.0011）已经花掉了。劝阻是概率，而概率会以固定比例失败。

    绕开"改闸 ① 判据"那条难走的路：闸 ① 保护的只是**元数据问题**（"这个库里有
    哪些表"），那类问题唯一能调的工具就是 search_schema。所以按问题类型分开：

      · metadata_only=True  —— 照旧摆上桌。闸 ① 要保护的正是这一类，行为与
        改动前逐字相同，上面那段论证护住的不变量原样成立。
      · metadata_only=False —— 收掉。数据问题必然要跑 execute_sql，闸 ① 天然
        满足，不存在"零工具调用直接作答"的情形。

    metadata_only 由预检产出（agent.IntentCheck），而且**判不准时它被要求填
    true**：误判成 true 的代价是多花一轮（退回改动前），误判成 false 的代价是
    元数据问题无工具可用。两个方向不对称，所以默认值与措辞都偏向 true。

    预检没跑（异常早退）时 state 里没有这一位，读到 False。那种情况下链路根本
    走不到 decide，不必为它单开一条分支。
    """
    if not state.get("schema_prompt"):
        return frozenset()                     # 召回什么都没给，该让它自己去搜
    if not state.get("schema_complete"):
        # 盲选 / 有表被预算裁掉：提示词里那份**不是**全部可用的表，
        # 两个检索工具都还有用武之地，一个都不撤。
        return frozenset()
    hide = {"get_table_schema"}
    if not state.get("metadata_only"):
        hide.add("search_schema")
    return frozenset(hide)


def _n_decide(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """模型自己挑下一步调哪个工具 —— 这一步是 agent 与老管道的**全部区别**。

    配额耗尽走 converged 而不是 rejected_by：已经查到的东西还在，该收敛作答，
    不是报错丢掉。
    """
    from .agent import (AGENT_USER, AgentAction, _render_history,
                        _sys, render_agent_system)

    d = _deps(config)
    step = state.get("step", 0) + 1
    human = AGENT_USER.format(
        schema=state.get("schema_prompt", ""), question=state["question"],
        history=_render_history(state.get("history") or []),
        steps_left=max(0, _step_cap(state) - step + 1))
    t = d.tracer.start()
    try:
        action, u = d.llm.structured(
            AgentAction,
            _sys(render_agent_system(d.cfg, _hidden_tools(state)), d.cfg),
            human)
    except QuotaExceeded as e:
        _llm_spans(d, "decide")
        d.tracer.add("decide", t, str(e), status="blocked")
        return {"step": step, "converged": "配额耗尽，收敛"}
    except Exception as e:                        # noqa: BLE001
        _llm_spans(d, "decide")
        d.tracer.add("decide", t, f"决策失败：{e}", status="failed")
        return {"step": step, "rejected_by": "LLM", "error": f"决策失败：{e}"}
    tok_used = state.get("tok_used", 0) + u.input_tokens + u.output_tokens

    # finish 与 tool 同时成立时**以工具为准**。
    #
    # AgentAction 的 finish / tool 是两个独立字段，没有互斥约束，模型经常两个
    # 一起给。路由原来只看 finish，那条 SQL 一次都没跑、链路上一个字都不留。
    #
    # 判据来自实测：2026-09-13 连跑四次「每个支付渠道各有多少笔」，三次以
    # UNGROUNDED 交白卷，而每一次 reflect 的 thought 写的都是工具那条路 ——
    # "用窗口函数 SUM(COUNT(*)) OVER () 让库算出总计，避免手算出错" ——
    # 药方每次都开对了，每次被丢掉，然后带着同一个手算错的合计再交一次卷。
    # 五次决策五次同一形状，没有反例：finish 是填 schema 的副产物，
    # 真实意图在 tool 上。
    #
    # 两道闸，缺一不可：
    #   · 工具名必须真实存在。模型胡诌一个工具名时，宁可按 finish 收尾，
    #     也不要把一次能交卷的链路送进"未知工具"的死胡同。
    #   · 必须还有预算。改走工具要多花一步；正好卡在上限上时，_after_act 会
    #     直接送去 finalize，而那时 answer 已经丢掉，本来能给的答案就没了。
    prefer_tool = bool(
        action.finish and action.tool and action.tool in tools.REGISTRY
        and step < _step_cap(state)
        and tok_used <= _tok_cap(state))
    finish = bool(action.finish) and not prefer_tool

    note = (action.thought or "")[:80]
    if prefer_tool:
        # 让这件事在 Span 列上看得见。不标 degraded —— 降级说的是系统没走主
        # 路径，这里是模型自己把两个互斥字段都填了，链路本身是健康的。
        note = f"[finish+tool → 按工具执行] {note}"[:110]

    sp = _llm_spans(d, "decide", u)
    d.tracer.add("decide", t, note,
                 stage=_decide_stage(action, state.get("history") or [],
                                     finish=finish),
                 **_sp_kw(sp))

    out: dict[str, Any] = {
        "step": step,
        "tok_used": tok_used,
        # 跑到 decide 就不再是快路径了 —— 回落进来的那些，state 里还留着
        # fast=True。不清掉的话 _n_finalize 会对一条完整链路的答案套用模板
        # 成句，把模型写好的归因盖掉。**路由函数改不了状态，只能在这里清。**
        "fast": False,
        # 决策结果进 state 供 _n_act 读。它是可序列化的普通 dict，不是
        # AgentAction 对象 —— 检查点存不下 pydantic 模型。
        # finish 写的是**归一之后**的值：_after_decide 读它来路由，两处各判
        # 一次就会出现"这里按工具走、那里按收尾走"。
        "action": {"finish": finish, "answer": action.answer or "",
                   "tool": action.tool or "", "args": dict(action.args or {})},
    }
    if finish:
        out["answer"] = action.answer or ""
        # 越界/负数在 _answer_exec 里退回"最后一次"，这里不做校验 —— 模型填错
        # 一个序号不该让整条链路失败。
        out["answer_step"] = int(getattr(action, "answer_step", 0) or 0)
    _check_handoff(d, state, step=step, tok_used=out["tok_used"])
    return out


#: 接地校验强制返工时一次性追加的预算。
#:
#: 一次改正要三步才走得完：decide（反思）→ act（去查）→ decide（拿真数重写）。
#: 而接地校验触发时往往已经烧掉大半预算，第三步经常没有 —— 2026-09-13 线上
#: trace e26935e37614 就是这样：SQL 真的跑了、真实合计也查回来了，模型却没有
#: 机会拿它重写结论，finalize 看见 ungrounded 非空照样拒答。查到了却来不及用，
#: 比没查更冤。
#:
#: 这笔额度**每次查询至多发一次**（grounding_retried 保证），所以上界是确定的：
#: 两次 decide 加一次工具调用。它买的不是"多探索一会儿"，而是护栏自己强制的
#: 返工 —— 那笔账不该记在用户的探索预算上。
#:
#: token 也要一起给。只放宽步数的话，这条链路会从"步数不够"变成"token 不够"，
#: 症状一模一样，等于没修。
GROUNDING_RETRY_STEPS = 2
GROUNDING_RETRY_TOKENS = 16_000


def _n_ground(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """数字接地校验：结论里的数追不追得到某一次查询结果。

    **老管道没有这一关。** 它拦的是这么一类失败：模型跑一条探查查询把"跑过了"
    那道门推开，然后把答案编出来（BUG-A5，实测答 134 万而真值 76.9 亿）。

    enforce 档先给**一次**改正机会：把追不到的数点名回灌，让模型自己去查。
    直接拒会把误判的代价全压在正确答案上，而多跑一轮只花一次调用。
    """
    from .agent import _grounding_mode, _known_constants

    d = _deps(config)
    gmode = _grounding_mode(d.cfg)
    answer = state.get("answer") or ""
    if gmode == "off" or not answer:
        return {"ungrounded": []}

    # 同 _n_act：计时器起在校验**之前**。grounding.ungrounded 要把答案里每个数
    # 拿去和全部 exec_results 比对，是这条链路上唯一一处纯 CPU 的重活，
    # 拿 add(start()) 记等于永远记 0，它慢起来时在链路上看不出来。
    gt = d.tracer.start()
    bad = grounding.ungrounded(answer, state.get("exec_results") or [],
                               known=_known_constants(d.cfg))
    if not bad:
        return {"ungrounded": []}

    ungrounded = [grounding.fmt([x]) for x in bad]
    d.tracer.add("grounding", gt,
                 f"结论里 {len(bad)} 个数追溯不到查询结果：{grounding.fmt(bad)}",
                 status="blocked" if gmode == "enforce" else "ok")
    if (gmode == "enforce" and not state.get("grounding_retried")
            and state.get("step", 0) < _step_cap(state) + GROUNDING_RETRY_STEPS):
        history = list(state.get("history") or [])
        history.append({
            "tool": GROUND_RETRY_TOOL, "args": {},
            "brief": f"**你的结论里这些数字没有出现在任何一次查询结果里："
                     f"{grounding.fmt(bad)}**。它们既不等于某个返回值，也不是"
                     f"两个返回值做一次加减乘除得到的。请先用 execute_sql 把它们"
                     f"真正查出来，再重写结论；确实查不到就如实说查不到，不要保留"
                     f"这些数字。"})
        # 清空 answer 是**回 decide 的信号**，别省 —— _after_ground 靠
        # "重试过 且 没答案"两条同时成立才放行，只判其中一个会转起来。
        # 连额度一起发下去。发在这里而不是 _after_act 里判 —— 那条边只知道
        # "超了没有"，不知道"为什么值得多给"。
        return {"ungrounded": ungrounded, "grounding_retried": True,
                "history": history, "answer": "",
                "steps_granted": int(state.get("steps_granted", 0))
                                 + GROUNDING_RETRY_STEPS,
                "tokens_granted": int(state.get("tokens_granted", 0))
                                  + GROUNDING_RETRY_TOKENS}
    return {"ungrounded": ungrounded}


#: 结果**只由参数决定**的工具 —— 只有这几个适用重复动作检测。
#:
#: analyze_result / export_result 吃的是 ctx.last_result（上一次 execute_sql 的
#: 结果），同一组参数在不同时刻指向的是不同的数据；把它们算作"重复"会把一次
#: 合法的再分析拦掉。判据是**结果依赖什么**，不是"看起来像不像同一次调用"。
_PURE_TOOLS = frozenset({"execute_sql", "search_schema", "get_table_schema"})


def _n_act(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """执行模型挑中的那个工具。**护栏最密的一个节点，四条缺一不可。**

    干跑与只读执行都在 tools.execute_sql **内部**，不单独落 span ——
    模型发出的是一次调用，界面上就该是一条 tool_call。
    """
    from .agent import _brief, _io_json

    d = _deps(config)
    act = state.get("action") or {}
    tool_name, args = act.get("tool") or "", dict(act.get("args") or {})

    # ⓪ 重复动作：同一组 (工具, 参数) 已经跑过就不再跑第二遍。
    #
    #    这个循环原本唯一的刹车是 max_steps，没有任何机制发现"在原地打转"。
    #    trace 26096703989b 里，接地校验把编造的数打回来之后，模型重发了一条与
    #    第 1 步**逐字节相同**的 SQL，拿回逐字节相同的结果，白烧一轮 —— 而它
    #    的 thought 写的是"补一次查询：拿到全部 9 个渠道的计数与总计"，计划是
    #    对的，动作没跟上。所以回灌里必须写清**该换成什么写法**：只说"你重复了"
    #    等于让它再猜一次，大概率原地再转一圈。
    #
    #    前提是只读查询在同一次执行的几秒内结果稳定。这条链路上的工具全是只读的
    #    （tools.REGISTRY），整轮通常 30 秒内跑完。将来若接入带副作用或对时效
    #    敏感的工具，这个判断要重新掂量 —— 那时应按工具白名单收窄，而不是撤掉。
    #    **只有上一次真的拿到结果才算重复。** 上一次失败（库超时、护栏拦下、
    #    模型写错了 SQL）时重发同一条是完全正当的重试 —— evals/chaos 注入一次
    #    数据库超时、模型重发同一条 SQL 恢复，正是这条链路的既定行为，按"见过
    #    就不跑"会把它判成沉默失败。老检查点的 history 没有 ok 这个键，
    #    `is True` 让它们落到"允许重跑"那一侧：放行一次多余的查询，
    #    比挡掉一次正当的重试轻得多。
    repeat_of = next(
        (i for i, h in enumerate(state.get("history") or [], 1)
         if h.get("tool") == tool_name and h.get("args") == args
         and h.get("ok") is True), None)
    if repeat_of is not None and tool_name in _PURE_TOOLS:
        rt = d.tracer.start()
        # 落 span 而不是静默跳过：省掉的这次调用要能在追踪页上看见，否则
        # "模型为什么没再查一次"这个问题在链路上没有答案。degraded 而非 ok ——
        # 它有产出，但不是主路径。
        d.tracer.add("tool_call", rt, f"与第 {repeat_of} 步完全相同，未重复执行",
                     status="degraded", tool=tool_name, input=_io_json(args))
        history = list(state.get("history") or [])
        history.append({
            "tool": tool_name, "args": args,
            "brief": (f"**这条与第 {repeat_of} 步完全相同，没有重新执行** —— "
                      "再跑一次拿回的还是同一份结果，未展示的行不会因此出现。"
                      "要拿到没看到的行，得改写 SQL：用 OFFSET 翻页、用 WHERE "
                      "缩小范围，或改成更聚合的写法（同一条 SQL 里把总计也选出来）。"
                      "确实拿不到就 finish=true 如实说明哪一部分没拿到，"
                      "**不要拿已看到的几行去外推**。"),
        })
        return {"step_count": state.get("step_count", 0) + 1, "history": history}

    # ⓪′ 召回已经做过的事不做第二遍。
    #
    #    _n_recall 调的就是 tools.search_schema(question)，结果全文已注入提示词。
    #    模型再调一次 search_schema 时，重跑的是一次 **embedding 计费调用 + 向量
    #    检索**，换回的是逐字相同的一份东西。数据问题上 _hidden_tools 已经把它
    #    从规格表里撤了，这里是硬兜底 —— 规格表是引导，这一条才是保证。
    #    元数据问题上它仍在桌上（闸 ① 要它），那一类正是这条兜底唯一还会
    #    真正拦到的场景。
    #
    #    不走上面那道 ⓪：那道闸按 (工具, 参数) 完全相同判，而召回这一次压根不在
    #    history 里，且模型填的 question 往往是自己的改写，字面对不上。
    if tool_name == "search_schema" and state.get("schema_prompt"):
        rt = d.tracer.start()
        hit = list(state.get("tables_hit") or [])
        d.tracer.add("tool_call", rt,
                     f"召回已在本次开始时完成（{len(hit)} 张表），未重复执行",
                     status="degraded", tool=tool_name, input=_io_json(args),
                     output=_io_json({"tables": hit, "reused": True}))
        history = list(state.get("history") or [])
        history.append({
            "tool": tool_name, "args": args, "ok": True,
            # columns 供 _meta_evidence 取证 —— "库里有哪些表"这类问题的依据
            # 就是这份表名清单，丢了它接地校验会把正确答案判成编造。
            "columns": hit,
            "brief": ("**本次召回在最开始就已经做过，没有重新执行** —— 拿回的就是"
                      "上文【可用的表】那一份，一字不差。要它之外的信息，"
                      "改用 execute_sql 去查数据，别再召回一遍。"),
        })
        return {"step_count": state.get("step_count", 0) + 1, "history": history}

    # 计时器必须在 invoke **之前**起。原来这两行是反的（先 invoke 再 start），
    # 于是每一条 tool_call span 的 ms 都由构造决定恒为 0 —— 界面上"数据库 0ms"
    # 不是数据库快，是这个读数根本没量。
    # 铁证：同一个 search_schema，在 _n_recall 里（计时器在调用前起）量到 331ms，
    # 在这里量到 0ms。execute_sql 还要过 AST 护栏 + 干跑 EXPLAIN + 只读执行 +
    # 脱敏，更不可能是 0。
    tt = d.tracer.start()
    res = tools.invoke(tool_name, args, d.ctx)
    # step 名**必须是静态的 tool_call**：复放/追踪页的步骤映射是静态表，
    # 动态 step id 会显示成原始串（见 tests/test_frontend 那两条护栏）。
    # 工具名进结构化 tool 字段，前端 Span 列直接读它。
    d.tracer.add("tool_call", tt, _brief(res),
                 status="ok" if res.ok else "blocked", tool=tool_name,
                 # 输入是模型填的那组参数，输出是工具返回的完整数据。
                 # _brief 只给一句"返回 10 行"，看不出返回的是哪 10 行。
                 input=_io_json(args),
                 output=_io_json(res.data) if res.ok
                        else f"{res.rejected_by or ''} {res.error}".strip())

    out: dict[str, Any] = {"step_count": state.get("step_count", 0) + 1}

    # ① 数据源根本连不上：重试没有价值，继续循环只会把 R-17 预算烧光，
    #    而烧光之后返回的是一句"未完全收敛"，用户看不出真因是库挂了。
    #    2026-09-11 跑测里宠物医疗源 10 条有 3 条这么烧掉、1 条据此编了答案。
    if res.data.get("fatal"):
        out.update({"rejected_by": "DATASOURCE", "error": res.error,
                    "hint": res.data.get("hint", "")
                            or "请在「数据源」页检查该源的连通性。"})
        return out

    history = list(state.get("history") or [])
    # ok 供上面的重复动作检测用：判"重跑会不会拿到新东西"，得先知道上一次
    # 到底拿到没拿到。
    item: dict[str, Any] = {"tool": tool_name, "args": args,
                            "brief": _brief(res), "ok": bool(res.ok)}

    # ② 高成本查询不直接拒，先给模型一次换写法的机会（HITL 之前的那一步）。
    #    原来这里直接 return：模型连"可以改用预聚合汇总表"都来不及试，而 R-11
    #    在生产数据规模下会挡掉大量最基本的问题（2026-09-11 跑测 26/130）。
    #    到收尾仍无成功执行时才按 R-11 挂审批，由 server._open_approval 建单。
    if res.rejected_by == "R-11":
        item["brief"] = (
            f"{res.error}。**这一版不能执行，换个更省的写法再试一次**："
            "① 优先改查同源的预聚合汇总表（表名多为 *_stats_daily / *_daily_stats），"
            "直接对汇总列求和；② 或加时间窗 / 主键区间过滤，分段统计后自行相加；"
            "③ 严禁用抽样（TABLESAMPLE、LIMIT 取样）冒充全量。"
            "若两条路都走不通，finish=true 并如实说明这个口径当前取不到。")
        history.append(item)
        out.update({"scan_blocked": dict(res.data or {}), "history": history})
        _check_handoff(d, state, step=state.get("step", 0),
                       explain_rows=int((res.data or {}).get("explain_rows") or 0))
        return out

    if tool_name == "execute_sql" and not res.ok:
        out["last_error"] = res.error or (res.rejected_by or "")

    # ③ 成功执行要**累加**进 exec_results，不是覆盖：模型的结论经常引用更早
    #    几步的数（"全表 447,000 条，其中可售 398,082"），只留最后一次会把
    #    大量正确答案判成编造。
    if res.ok and tool_name == "execute_sql":
        out["last_exec"] = dict(res.data or {})
        # 带上**步号与完整返回**：结果区要按模型指认的 answer_step 回头取某一步的
        # 结果（见 _answer_exec），只留 columns/rows 拼不回 as_of / explain_rows /
        # masked_columns 这些 AskResult 要的字段。
        # 接地校验只读 columns/rows（grounding.values_of / _subset_sum_keys /
        # _text_digit_keys 三处都只取 r["rows"]），多出来的键对它是透明的。
        out["exec_results"] = list(state.get("exec_results") or []) + [
            {**dict(res.data or {}),
             "columns": list(res.data.get("columns") or []),
             "rows": list(res.data.get("rows") or []),
             # history 是 1 起数的（_render_history 用 enumerate(history, 1)），
             # 而这条记录是 append 之后的那一项 —— 与提示词里模型看到的序号对齐。
             "step": len(state.get("history") or []) + 1}]
        # ④ 供 analyze_result / export_result 用。ctx 不进检查点，它是本次
        #    执行的现场；续跑时从 history 重建不了，那两个工具因此只在
        #    同一次执行内可用 —— 与改造前一致。
        d.ctx.last_result = res.data
        pv = planner.preview_rows(res.data.get("rows", []))
        item["preview"] = {"columns": res.data.get("columns", []),
                           "rows": pv,
                           "row_count": res.data.get("row_count")}
        # 预览被裁掉时才带统计 —— 行全给到了就不必再贴一份（见 _render_history）。
        if len(pv) < int(res.data.get("row_count") or 0):
            item["stats"] = _stats_line(res.data.get("column_stats") or [])
    elif res.ok and tool_name == "get_table_schema":
        item["columns"] = [c["name"] for c in res.data.get("columns", [])]
        # 查的是上文已经逐字列出的表 —— 这一步没带来任何新信息，白花了一轮决策。
        # 不拒（拒会再多烧一轮），但要在回灌里点破，别让它养成"先确认一遍"的习惯。
        if str(res.data.get("table") or "") in set(state.get("tables_hit") or []):
            item["brief"] += ("（**这张表的结构在上文【可用的表】里已经逐字列出**，"
                              "这次查询没有带来任何新信息 —— 上文已列出的表直接照着用，"
                              "不要再查一遍）")
    elif res.ok and tool_name == "search_schema":
        item["columns"] = res.data.get("tables", [])

    history.append(item)
    out["history"] = history
    _check_handoff(d, state, step=state.get("step", 0),
                   explain_rows=int(res.data.get("explain_rows") or 0)
                                if isinstance(res.data, dict) else 0)
    return out


def _answer_exec(state: AgentState) -> dict[str, Any] | None:
    """结果区该渲染**哪一次**执行的结果。

    此前恒取 last_exec，也就是"最后执行"那一条 —— 而多步链路里最后执行的往往是
    探查或核对，不是回答问题的那一条。生产 trace 4bac5ce7f21b 就是这个形状：
    第 5 步那条 GROUP BY 才是答案，第 11 步那条加了"近 3 月均值"的改写版只是
    顺手多给的，结果区却渲染后者。evals/baseline.py 的注释里也记着同一件事的
    另一面 —— 审计里的 rows_returned 取的是最后执行那条，拿它反查答案 SQL
    会选中探查语句。

    **改提示词修不好它**（试过），因为这不是模型不懂，是消费端一直没问过它。
    所以让它在 finish 那一次直接指认：AgentAction.answer_step 填
    【已完成的工具调用与结果】里的序号，这里按号回取。

    三种情况一律退回"最后一次执行"，因为那正是改动前的行为 —— 这条改动**只在
    模型明确指认且指认得到时**改变结果，其余时刻逐字不变：
      · 没填（0）；
      · 填了但那一步不是成功的 execute_sql（探查失败、或指到工具调用上）；
      · 越界 / 负数。
    """
    execs = state.get("exec_results") or []
    if not execs:
        return state.get("last_exec")
    want = int(state.get("answer_step") or 0)
    if want > 0:
        hit = next((e for e in execs if int(e.get("step") or 0) == want), None)
        if hit is not None:
            return hit
    return state.get("last_exec")


#: 一行统计最多占多少字符。宽结果（几十列）上整份统计能顶掉大半个预览预算，
#: 而回灌它的目的只是"别让模型对看不见的行瞎猜"，不是给它一份完整报表。
_STATS_CHARS = 400


def _stats_line(stats: list[dict[str, Any]]) -> str:
    """列级统计压成一行。只留对"整列长什么样"真正有用的那几项。"""
    bits = []
    for st in stats:
        parts = [f"非空 {st.get('count', 0)}", f"去重 {st.get('distinct', 0)}"]
        if "min" in st:
            parts.append(f"min {st['min']:g}/max {st['max']:g}/均值 {st['mean']:g}")
        if st.get("note"):
            parts.append(st["note"])
        bits.append(f"{st.get('column', '')}[{'，'.join(parts)}]")
    line = "；".join(bits)
    return line if len(line) <= _STATS_CHARS else line[:_STATS_CHARS] + " …（统计已截断）"


def _meta_evidence(state: AgentState, cfg: Config | None = None,
                   ) -> list[dict[str, Any]]:
    """把**非 execute_sql** 的成功工具返回也折成可核对的证据。

    search_schema 返回表清单、get_table_schema 返回列清单 —— 这些是模型回答
    "库里有哪些表""某张表有哪些字段"时的真实依据，只是它们不经 execute_sql，
    exec_results 里一条都没有。不把它们算作证据，这类问题就只能被判成编造。

    除了名字本身，**还把清单长度一并给出**：模型说"共 8 张表"时，那个 8 正是
    len(tables)，它在返回值里不作为一个元素存在，只作为个数存在。
    """
    out: list[dict[str, Any]] = []
    # _n_recall 那一次召回也是**真跑过的工具返回**（它调的就是 tools.search_schema），
    # 只是执行者是图不是模型，于是它一直不在 history 里 —— 证据从前全靠模型自己
    # 再调一次 search_schema 才进得来。补上它是因为那条路已经不保险了：
    # 提示词改过之后模型更可能直接照注入的 schema 作答，而 _n_act 的 ⓪′ 又会把
    # 重复召回折成复用。少了这一条，"库里有哪些表"的依据就只剩运气。
    #
    # **2026-09-15 起这一条也参与闸 ①，但只对 metadata_only 那一类**（见
    # _n_finalize 里 meta_backed 那段）。原来这里写的是"不影响闸 ①"，理由是
    # 让召回顶替闸 ① 会放过"大约 120 万条订单"那种编造 —— 那个理由今天仍然
    # 成立，所以放开的口子按预检的 metadata_only 收窄，数据问题一步没松。
    hit = list(state.get("tables_hit") or [])
    if hit:
        out.append({"columns": ["name"], "rows": [[n] for n in hit]})
        out.append({"columns": ["count"], "rows": [[len(hit)]]})
        # 召回回来的**列清单**也是证据，而且是这一类问题最常引用的那份。
        #
        # 少了它，闸 ① 放行的元数据问题会原地落进闸 ② 再被拒一次 —— 拒答的
        # 位置从第 ① 道挪到第 ② 道，用户看到的还是拒答。会踩到的是答案里引用了
        # 列定义里某个 ≥ MIN_ABS 的数的场合（VARCHAR(2048)、DECIMAL 精度、
        # 枚举取值），不常见但不是不会有。
        #
        # 取的就是 get_table_schema 本该返回的那份 —— 而那个工具正是提示词
        # 劝模型别调的。劝它别调，就得把它的返回值替它补上，否则又是一次
        # "规则打架、用户挨打"。
        for name in hit:
            spec = (cfg.tables.get(name.lower()) if cfg else None)
            cols = list(spec.columns) if spec else []
            if cols:
                out.append({"columns": ["name"], "rows": [[c] for c in cols]})
                out.append({"columns": ["count"], "rows": [[len(cols)]]})
    for h in state.get("history") or []:
        if not h.get("ok") or h.get("tool") == "execute_sql":
            continue
        names = list(h.get("columns") or [])
        if not names:
            continue
        out.append({"columns": ["name"], "rows": [[n] for n in names]})
        out.append({"columns": ["count"], "rows": [[len(names)]]})
    return out


#: 快路径成句时最多逐条列出几行。再多就只报条数、让用户看结果表 ——
#: 一段列了二十行的"结论"不是结论。
_FAST_BULLETS = 5


def _fmt_cell(v: Any) -> str:
    """一个结果格子写成人话。**只做格式，不做换算** —— 换算就是编造的开始。"""
    if v is None:
        return "空"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        # 整值浮点（COUNT 经某些驱动回来是 float）按整数写，否则留两位。
        # 千分位一起加上：这一格用户要直接读，128000 和 128,000 的差别很实际。
        return f"{int(v):,}" if v == int(v) else f"{v:,.2f}"
    return str(v)


def _fast_answer(state: AgentState, exec_data: dict[str, Any]) -> str:
    """快路径的结论句。**纯拼装，一个模型 token 都不花。**

    每个数字都逐字取自 exec_data 的结果行，不经任何转述或换算 —— 这是这条
    路径敢跳过收尾决策的全部理由。口径那句来自 _n_fast（它看过 schema），
    数字来自库，两者都不是这里现编的。
    """
    label = (state.get("fast_label") or "").strip() or "查询结果"
    caliber = (state.get("fast_caliber") or "").strip()
    cols = [str(c) for c in (exec_data.get("columns") or [])]
    rows = list(exec_data.get("rows") or [])

    def cells(row: Any) -> list[str]:
        if isinstance(row, dict):
            return [_fmt_cell(row.get(c)) for c in cols]
        if isinstance(row, (list, tuple)):
            return [_fmt_cell(v) for v in row]
        return [_fmt_cell(row)]

    parts: list[str] = []
    if caliber:
        parts.append(f"**口径**：{caliber}")

    if len(rows) == 1 and len(cols) <= 1:
        parts.append(f"**结论**：{label}为 **{cells(rows[0])[0]}**。")
    elif len(rows) == 1:
        pairs = "；".join(f"{c} {v}" for c, v in zip(cols, cells(rows[0])))
        parts.append(f"**结论**：{label} —— {pairs}。")
    else:
        parts.append(f"**结论**：{label}共 {len(rows):,} 条，完整结果见下方结果表。")
        bullets = []
        for row in rows[:_FAST_BULLETS]:
            vs = cells(row)
            bullets.append("- " + "；".join(
                f"{c} {v}" for c, v in zip(cols, vs)) if cols else "- " + "；".join(vs))
        if bullets:
            parts.append("\n".join(bullets))
        if len(rows) > _FAST_BULLETS:
            # **把省略说出来。** 不说的话，列出的这几行会被读成全部 ——
            # 那正是这套界面反复要消灭的那种静默收窄。
            parts.append(f"（以上为前 {_FAST_BULLETS} 条，其余见结果表）")
    return "\n\n".join(parts)


def _n_finalize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """收尾：能不能把这个答案给用户。**纯代码判定，不问模型。**

    原来只有一句 `ok = bool(last_exec) or bool(answer)` —— 只要模型吐了字就算
    成功，于是"一次 SQL 都没发、直接写个整数再补一段口径说明"照样 ok=true。
    2026-09-11 跑测 130 条里有 15 条是这么来的（订单总数答 1 万、实际 120 万），
    界面上无从分辨。

    **下面几条的顺序不能换**，它是一串短路 if。

    收敛理由（达步数上限 / token 触顶）在这里用 _converge_reason 现算 ——
    判据与 _after_act 共用一份，见那个函数的说明。
    """
    from .agent import (_grounding_mode, _grounding_no_evidence_mode,
                        _has_number, _io_json, _known_constants,
                        metadata_recall_is_evidence)

    d = _deps(config)
    t = d.tracer.start()

    def _verdict(note: str, status: str = "blocked", **out: Any) -> dict[str, Any]:
        """收尾判定落一条 span，再把判定本身返回。

        **这一步此前一条 span 都不落，而枪毙答案的判定恰恰发生在这里。**
        症状是链路上三行全绿、最后一行还写着"证据充分，可给出结论"，用户却
        什么都没拿到 —— 2026-09-13 线上 trace fd3711604c2f 就是这样：模型
        一次 execute_sql 都没跑，直接编了一张五渠道的表（真实 9 个渠道、
        总计编成 1,027,010 而真值 1,122,911），NO_EVIDENCE 把它拦下了，
        而拦这件事在 Span 明细里看不见。
        对照 grounding：它落 span，所以"结论里 N 个数追溯不到"看得见。

        output 里带上结构化判定（rejected_by / error / hint），排查时不用
        再去猜 note 那句话对应哪个分支。
        """
        d.tracer.add("finalize", t, note, status=status,
                     output=_io_json(out) if out else None)
        return out

    # 早退（配额 / LLM 故障 / 越界 / 库挂了）原样带出去，不再二次判定。
    # **落 ok 不落 blocked**：真正失败的是上游那一步，它自己已经有一条红的
    # span；这里再红一次，一次故障在界面上会变成两次。
    if state.get("rejected_by"):
        d.tracer.add("finalize", t,
                     f"上游已判定 {state.get('rejected_by')}，不再二次判定")
        return {}

    gmode = _grounding_mode(d.cfg)
    last_exec = state.get("last_exec")
    ungrounded = state.get("ungrounded") or []
    answer = state.get("answer") or ""
    # **在这里算，不能只读 state**：被上限挡住时是条件边把流程送来的，而
    # LangGraph 的路由函数改不了状态 —— 只读 state 的话，"达步数上限"这句
    # 永远拼不进答案，用户看到的是一个不知为何不完整的结果。
    converged = state.get("converged") or _converge_reason(state)

    if last_exec is not None and ungrounded and gmode == "enforce":
        # 给过一次改正机会仍追溯不到 —— 这些数不是从库里来的，不能递出去。
        # 与 NO_EVIDENCE 分开：那条是"一次都没跑"，这条是"跑了但答案没用上"。
        return _verdict(
            f"结论里 {len(ungrounded)} 个数追溯不到查询结果，给过一次改正机会"
            f"仍未补上，不给出这个答案",
            rejected_by="UNGROUNDED",
            error=f"结论里这些数字追溯不到任何一次查询结果："
                  f"{'、'.join(ungrounded)}，因此不给出这个答案。",
            hint="换个更具体的问法，或在「直查 SQL」里自己跑一条核对；"
                 "结果表仍在下方，可直接看。")

    if last_exec is not None:
        # 快路径成句：**用模板，不问模型。** 这是省掉收尾那次调用的地方，
        # 也是整条改动里收益最大的一刀 —— 生产实测收尾 decide 平均 4.3 秒
        # （输出 277 token，按 ms≈1170+14.4×out 几乎全在生成那段口径文字上）。
        #
        # 数字全部逐字取自刚刚返回的结果行，不经模型转述，所以它**天然接地**：
        # grounding.ungrounded 拿去比对必然为空，UNGROUNDED 那道门对这条路径
        # 是个恒真判定。这不是绕过校验，是这条路径上根本没有可编造的环节。
        if state.get("fast") and not answer:
            answer = _fast_answer(state, last_exec)
        if not answer:
            answer = (("（未在预算内完成归因）" + converged + "。") if converged else "") + \
                     "以下为最后一次查询执行的原始结果，请直接看结果表。"
        # 成功也要落一格。链路末尾永远缺最后一步的话，看的人不知道收尾到底
        # 做了什么判定 —— "没有坏消息"和"没有这一步"长得一模一样。
        d.tracer.add("finalize", t,
                     "已附最终结果与口径" + (f"；{converged}" if converged else ""))
        return {"answer": answer}

    # 以下都是"本轮没有一次成功的 execute_sql"。
    if state.get("scan_blocked") is not None:
        # 换过写法仍然过不去：按 R-11 挂审批，交由 server 建单。
        sb = state.get("scan_blocked") or {}
        return _verdict(
            "换过写法仍超扫描上限，按 R-11 挂审批",
            rejected_by="R-11", last_exec=sb,
            error=sb.get("error") or "预估扫描量超过阈值，需人工放行")

    tail = (f"（最后一次查询执行失败：{state.get('last_error')}）"
            if state.get("last_error") else "")
    if _has_number(answer):
        # **P0 兜底**：没取到数据就不许出数字。这里刻意不把模型那段话回显给
        # 用户 —— 它正是编造出来的内容，回显等于换个位置继续骗人。
        #
        # 但判据不能是"有任何一个数字就拒"。_has_number 匹配的是**单个字符**：
        # 有序列表的 "1." "2."、表名里的 payment_stats_daily…… 统统算数。于是
        # "这个库里有哪些表"这类**只靠元数据就能如实回答**的问题被系统性误杀 ——
        # 2026-09-13 线上 9d6bccacd61f：模型规规矩矩用 search_schema 召回 8 张表、
        # 逐张列出并注明"以召回结果为准"，一个数据数字都没编，照样判 NO_EVIDENCE。
        #
        # 收紧成两档：
        ran_tool = any(h.get("ok") for h in (state.get("history") or []))
        # 元数据问题的依据是**图自己那次召回**，不是模型有没有再调一遍。
        #
        # 2026-09-15 线上 f9d0a062165a：问"会员表字段结构"，预检判
        # metadata_only=True，召回把 13 张表的完整列定义注进了提示词，模型据此
        # 逐列答出来 —— 一个数据数字都没编，照样被闸 ① 一刀切拒。它的
        # thought 写得明明白白："schema 召回已给出 customers 与 member_levels
        # 的完整列定义，直接据此回答。"
        #
        # 这不是模型偷懒，**是提示词要求它这么做的**：AGENT_SYSTEM 第一条写着
        # "不要为了'确认一下'再查一遍它已经写明的东西"，AGENT_USER 开头再写一遍
        # "下面已经列出的表不要再调 get_table_schema"。提示词让它别查，闸 ① 又
        # 因为它没查而拒答 —— 两条规则互相打架，而挨打的是用户。
        #
        # _hidden_tools 里"metadata_only=True 照旧把 search_schema 摆上桌"那句
        # 留的是**概率**逃生口：桌上有这个工具，不等于模型会去调它，何况提示词
        # 正在劝它别调。这次就是劝住了。所以判据要从"模型动没动手"改成
        # "有没有依据"。
        #
        # **收窄到 metadata_only 这一类，不是普遍放开。** _meta_evidence 的注释
        # 里记着为什么当初没让召回顶替闸 ①：会放过"大约有 120 万条订单"这种
        # 小于 grounding.MIN_ABS 的编造（tests/test_agent_runtime.py::
        # test_agent_no_evidence_blocks_fabricated_number 盯着这条）。那条用例的
        # 问题是"订单总数"，预检判 metadata_only=False —— 数据问题一步都没放松，
        # 闸 ① 照旧一刀切。metadata_only 这个信号是 2026-09-15 才有的，当初
        # 试这条路时它还不存在。
        #
        # 放行的也只是**闸 ①**：元数据问题接着落到下面的闸 ②，每个大额数字仍要
        # 在 _meta_evidence 里逐个追溯，追不到照样拒。
        meta_backed = (metadata_recall_is_evidence(d.cfg)
                       and bool(state.get("metadata_only"))
                       and bool(state.get("tables_hit")))
        if not ran_tool and not meta_backed:
            # ① 一次工具都没成功调用过 —— 纯凭空作答，照旧一刀切拒。
            #    2026-09-13 d09d099a2209 正是这样：零次工具调用，直接编出一张
            #    退款表（WECHAT 10,432 笔 / 1,978,562.34 元），还写着"通过
            #    payment_no 关联 payments.channel_code"，那条 SQL 从不存在。
            return _verdict(
                "本轮一次工具都没调用成功，结论里的数字没有来源，"
                "不给出这个答案",
                rejected_by="NO_EVIDENCE", answer="",
                error="本轮没有任何一次查询执行成功，因此不给出带数字的结论。" + tail,
                hint="换个更具体的问法，或先确认这个口径需要的表是否可查；"
                     "也可在「直查 SQL」里自己跑一条核对。")
        # ② 调用成功过（哪怕只是 search_schema）—— 逐个数字核对来源，追不到才拒。
        #    判据与 grounding 共用一份，不另起一套阈值：那一层已经按"宁可漏、
        #    不可误"标定过（只查大额数、跳过年份占比、允许一次算术），
        #    这里再写一套迟早两边漂开。
        ne_mode = _grounding_no_evidence_mode(d.cfg)
        bad = (grounding.ungrounded(answer, _meta_evidence(state, d.cfg),
                                    known=_known_constants(d.cfg))
               if ne_mode != "off" else [])
        if bad and ne_mode == "enforce":
            return _verdict(
                f"没有一次 execute_sql 跑成，结论里 {len(bad)} 个数也追溯不到"
                f"任何一次工具返回，不给出这个答案",
                rejected_by="NO_EVIDENCE", answer="",
                error=f"本轮没有任何一次查询执行成功，而结论里这些数字追溯不到"
                      f"工具返回：{grounding.fmt(bad)}。" + tail,
                hint="换个更具体的问法，或先确认这个口径需要的表是否可查；"
                     "也可在「直查 SQL」里自己跑一条核对。")
        if bad:
            # shadow：不拦，只记一格并把这些数带进 ungrounded_numbers 供观测 ——
            # 这一档会误杀正确的基础统计（D-3），先在真实流量上量误判率再决定
            # 切不切 enforce（见 _grounding_no_evidence_mode）。
            d.tracer.add("finalize", t,
                         f"接地-NO_EVIDENCE 影子档：本会拒 {len(bad)} 个追溯不到"
                         f"工具返回的数（{grounding.fmt(bad)}），已放行观测")
            return {"answer": answer,
                    "ungrounded": [grounding.fmt([x]) for x in bad]}
        d.tracer.add("finalize", t,
                     "没跑 SQL，但结论里的数都来自工具返回（表清单 / 字段清单），放行")
        return {"answer": answer}
    if not answer:
        return _verdict("没有任何一次成功查询，模型也没给出结论",
                        rejected_by="NO_RESULT", error="未能产出结果" + tail)
    # 不含任何数字的定性回答（"这个库里有哪些表"）没有可编造的量，放行。
    d.tracer.add("finalize", t, "定性回答，不含可编造的数字，放行")
    return {"answer": answer}


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def _after_recall(state: AgentState) -> Literal["fast", "intent"]:
    """召回之后分流：这题走不走快路径。**判定已在 _n_recall 里落过 span。**

    路由函数改不了状态（LangGraph 的约束），所以判定本身在节点里做、
    结论写进 state，这里只读那一位。两处各判一次就会漂。
    """
    return "fast" if state.get("fast") else "intent"


def _after_fast(state: AgentState) -> Literal["act", "decide", "intent", "finalize"]:
    """快路径之后四条路。回落分两种，**区别在预检做没做过**。

    · 拒了（越域 / 不可答 / 配额）  —— 直接收尾，与预检拒答同一条口径。
    · 产出了 SQL                  —— 去执行。
    · 模型判 too_complex          —— 预检**已经做过**（越域、可答性两位都已
      判定并放行），直接进 decide。这一条是回落路径不退化的关键：fast 顶掉了
      intent 那一次调用，完整链路照跑，总次数与改动前持平。
    · 调用本身失败                 —— 预检没做过，走 intent 补上。OOS / CLARIFY
      两道门不能因为一次加速尝试失败就消失。
    """
    if state.get("rejected_by"):
        return "finalize"
    if (state.get("action") or {}).get("tool"):
        return "act"
    return "decide" if state.get("prechecked") else "intent"


def _after_intent(state: AgentState) -> Literal["decide", "finalize"]:
    return "finalize" if state.get("rejected_by") else "decide"


def _after_decide(state: AgentState) -> Literal["act", "ground", "finalize"]:
    if state.get("rejected_by") or state.get("converged"):
        return "finalize"
    return "ground" if (state.get("action") or {}).get("finish") else "act"


def _after_ground(state: AgentState) -> Literal["decide", "finalize"]:
    """接地校验给过改正机会（answer 被清空）就回去再决策一轮，否则收尾。

    **两条必须同时判**：只看 grounding_retried，第二次进来仍会回 decide，
    图就转起来了；只看 answer 为空，第一轮还没答案时也会误判。
    """
    return ("decide" if state.get("grounding_retried") and not state.get("answer")
            else "finalize")


def _after_act(state: AgentState) -> Literal["decide", "finalize"]:
    """R-16 步数与 R-17 预算都判在这里 —— 判完才决定要不要再来一轮。

    放在这条边上而不是 decide 入口：超限时能省掉一次没有意义的模型调用。
    收敛理由写进 converged，finalize 会把它缀进答案，让人知道这个结果是
    "跑完了"还是"跑不动了"。
    """
    if state.get("rejected_by"):
        return "finalize"
    # 快路径：结果的形状说了算，不是判据说了算。够直接成句就收尾（整条链路
    # 一次模型调用），不够就当作没走过快路径、交回 decide —— 见 fast_result_ok。
    if state.get("fast") and not fast_result_ok(state.get("last_exec")):
        return "finalize"
    return "finalize" if _converge_reason(state) else "decide"


def _step_cap(state: AgentState) -> int:
    """R-16 这一轮实际能跑到第几步（含接地校验追加的那点额度）。"""
    return int(state.get("max_steps", 0)) + int(state.get("steps_granted", 0))


def _tok_cap(state: AgentState) -> int:
    """R-17 这一轮实际的 token 上限（同上）。"""
    return int(state.get("cost_cap", 0)) + int(state.get("tokens_granted", 0))


def _converge_reason(state: AgentState) -> str:
    """为什么不再往下跑。空串 = 不是被上限挡住的。

    **这是 R-16 / R-17 判据的唯一一份。** `_after_act` 直接读它的真假，
    不再自己写一遍 —— 原来两处各判一次，改一处忘另一处就会出现"提前收敛了
    但没说为什么"，而那正是这个函数的存在理由。

    报出来的仍是**用户配的那个数**，不含追加额度：说"达步数上限 6"而实际
    跑了 8 步，比说"上限 8"清楚 —— 前者对得上配置文件，后者对不上任何东西。
    """
    if state.get("step", 0) >= _step_cap(state):
        return f"达步数上限 {state.get('max_steps')}，收敛作答"
    if state.get("tok_used", 0) > _tok_cap(state):
        return f"累计 token 超预算 {state.get('cost_cap')}，收敛作答"
    return ""



def build_skeleton() -> StateGraph:
    g = StateGraph(AgentState)
    for name, fn in(("recall", _n_recall), ("intent", _n_intent),
                    ("fast", _n_fast),
                    ("decide", _n_decide), ("act", _n_act),
                    ("ground", _n_ground), ("finalize", _n_finalize)):
        g.add_node(name, fn)
    g.set_entry_point("recall")
    g.add_conditional_edges("recall", _after_recall, {...})
    g.add_conditional_edges("fast", _after_fast, {...})
    g.add_conditional_edges("intent", _after_intent, {...})
    g.add_conditional_edges("decide", _after_decide, {...})
    g.add_conditional_edges("act", _after_act, {...})
    g.add_conditional_edges("ground", _after_ground, {...})
    g.add_edge("finalize", END)
    return g

# ---------------------------------------------------------------------------
# 编译与出入口
# ---------------------------------------------------------------------------

_GRAPH = None
_GRAPH_KEY: str | None = None


def ensure_graph(cfg: Config):
    """编译好的 agent 图，按检查点落点缓存。

    **检查点基建整个复用 graph.build_graph**（SQLite+WAL / PostgresSaver 两条路
    都在那儿处理好了）。这里只换骨架，不另写一份 saver —— 两份 saver 配置就会
    出现"审计在库里、检查点还在某台机器的本地盘上"，而一次失败复现要两者对得上。

    缓存键与 graph._ensure_graph 同一口径：换库/换文件都要重编，否则进程里
    还拿着上一个 saver。
    """
    from . import auditstore, graph as _graph

    global _GRAPH, _GRAPH_KEY
    key = ("pg:" + _graph._pg_key()) if auditstore.enabled(cfg) else str(cfg.checkpoint_db)
    if _GRAPH is None or _GRAPH_KEY != key:
        _GRAPH = _graph.compile_with_checkpoint(build_skeleton(), cfg)
        _GRAPH_KEY = key
    return _GRAPH


def reset_graph() -> None:
    """丢掉缓存的图。测试里换检查点落点时要调 —— 与 graph 那边同一个理由。"""
    global _GRAPH, _GRAPH_KEY
    _GRAPH, _GRAPH_KEY = None, None


def initial_state(question: str, org: int, trace_id: str, thread_id: str,
                  max_steps: int, cost_cap: int,
                  clarification: str = "") -> AgentState:
    """一次新执行的起始状态。

    clarification 走 history 而**不是改写 question**：question 是这条线程的身份
    （审计标题、审批指纹都读它），就地改掉会让同一条线程在界面上变成另一个问题。
    补充作为"已知条件"摆进历史，模型第一轮决策就看得到。
    """
    history: list[dict[str, Any]] = []
    extra = (clarification or "").strip()
    if extra:
        history.append({
            "tool": "(发起人补充)", "args": {},
            "brief": f"发起人补充的条件 —— 优先按它确定时间范围、口径与统计维度：{extra}"})
    return {
        "question": question, "org_id": org,
        "trace_id": trace_id, "thread_id": thread_id,
        "schema_prompt": "", "schema_heads": "", "tables_hit": [],
        "history": history, "exec_results": [],
        "last_exec": None, "scan_blocked": None, "last_error": "",
        "answer": "", "answer_step": 0, "converged": "", "step": 0, "step_count": 0,
        "ungrounded": [], "grounding_retried": False,
        "rejected_by": None, "error": "", "hint": "", "reasoning": "",
        "max_steps": max_steps, "cost_cap": cost_cap, "tok_used": 0,
    }


def to_result(state: AgentState, cfg: Config, tracer: Tracer):
    """图的终态 → 对外的 AskResult。

    形状必须与管道**完全一致**（走同一个 agent._result）：server 与前端都按
    AskResult 读，多一个字段少一个字段都是契约变更。
    """
    from .agent import _result

    # converged 在 _after_act 判、在这里补 —— 两处判据写在 _converge_reason 里，
    # 只有一份。
    converged = state.get("converged") or _converge_reason(state)
    return _result(
        cfg, state["question"], state["trace_id"], state["thread_id"],
        int(state.get("org_id", 0)), tracer,
        ok=not state.get("rejected_by"),
        reasoning=state.get("answer") or state.get("reasoning") or "",
        # **不是 state["last_exec"]** —— 结果区要渲染的是"回答问题那一条"的结果，
        # 不是"最后执行"那一条。没指认时 _answer_exec 退回 last_exec，行为不变。
        last_exec=_answer_exec(state),
        exec_results=state.get("exec_results") or [],
        rejected_by=state.get("rejected_by"),
        error=state.get("error", ""), hint=state.get("hint", ""),
        tables_hit=state.get("tables_hit") or [],
        step_count=max(1, int(state.get("step_count", 0))),
        converged=converged,
        ungrounded=state.get("ungrounded") or [],
    )


def recursion_limit(max_steps: int) -> int:
    """LangGraph 的递归上限。

    这张图**有环**（act → decide），默认 25 步在 max_steps=6 时会被环乘开，
    跑到一半抛 GraphRecursionError —— 那是个框架异常，用户看到的会是 500，
    而不是"达步数上限，收敛作答"。一轮最多经过 decide/act/ground 三个节点，
    再留一点余量给 recall/intent/finalize。
    """
    return max_steps * 4 + 10


# ---------------------------------------------------------------------------
# 续跑与复放
#
# 2026-09-12 从 graph.py 搬过来：检查点现在由**这张图**在写，续跑/复放自然也
# 要拿这张图去恢复。留在那边的话，会用老管道的图去读 agent 写的 state ——
# thread_id 对得上、字段对不上，症状是"任务中心说能续、点下去 404"。
# ---------------------------------------------------------------------------

def is_resumable(thread_id: str, cfg: Config) -> bool | None:
    """这条线程现在还能不能续跑。True/False；查不到检查点库时返回 None。

    判定与 resume() 同源（values 有、next 非空），避免"任务中心说能续、
    点下去 404"这种分叉 —— 审计记录只知道上次以什么收尾，不知道现场到底
    有没有落盘，也不知道后来是不是已经被续跑跑完了。
    """
    try:
        snap = ensure_graph(cfg).get_state(
            {"configurable": {"thread_id": thread_id}})
    except Exception:                 # noqa: BLE001
        return None
    return bool(snap.values and snap.next)


def progress(thread_id: str, cfg: Config) -> dict[str, Any] | None:
    """这条线程此刻跑到哪儿了 —— 第几步、正在用哪个工具。

    **取自检查点，不新增审计记录。** 图每过一个节点就落一次检查点，那里本就
    记着 step 与 action；为"看得见进度"再往审计里写一串中间记录，等于让统计、
    任务聚合、复核队列全部跟着变形，而它们的口径正是这个仓库最容易漂的东西。

    查不到检查点（还没落第一个节点 / 检查点库异常）返回 None ——
    调用方据此显示"正在执行"，而不是编一个第 0 步出来。
    """
    try:
        snap = ensure_graph(cfg).get_state(
            {"configurable": {"thread_id": thread_id}})
    except Exception:                 # noqa: BLE001
        return None
    values = snap.values or {}
    if not values:
        return None
    action = values.get("action") or {}
    return {
        "step": int(values.get("step", 0) or 0),
        "max_steps": int(values.get("max_steps", 0) or 0),
        "tool": str(action.get("tool") or ""),
        "next": list(snap.next or ()),
    }


def replay(trace_id: str, cfg: Config) -> list[dict[str, Any]]:
    """取回某次调用的全部检查点快照，用于失败复现与归因。"""
    out: list[dict[str, Any]] = []
    for snap in ensure_graph(cfg).get_state_history(
            {"configurable": {"thread_id": trace_id}}):
        v = snap.values or {}
        out.append({
            "next": list(snap.next or ()),
            "step": v.get("step"),
            "tool": (v.get("action") or {}).get("tool", ""),
            "answer": (v.get("answer") or "")[:200],
            "error": v.get("error") or "",
            "rejected_by": v.get("rejected_by"),
            "tok_used": v.get("tok_used"),
        })
    return out


def resume(thread_id: str, cfg: Config,
           executor: Executor | None = None, llm: LlmClient | None = None,
           *, clarification: str = "", question: str = "",
           org_id: int | None = None, handoff: Any = None):
    """把一条停下来的线程往前推一步。

    **两条路，判据是现场还在不在检查点里：**

    1. ``snap.next`` 非空 —— 真正的中断（进程被杀）。从最后一个完成的节点接着
       跑，已完成的节点不重跑。
    2. 没有活检查点，但调用方给了 ``question`` —— 这条线程正常收尾了，只是收在
       "还有下一步"的档上（等待补充 / 已拦截 / 复核未通过）。带着新输入重跑
       整条链路。

    两条都写**新的 trace_id、同一个 thread_id**，审计里因此看得出这是第几次
    执行；另计一次每日配额 —— 补充一次就是一次真实的模型消费，不能白送。

    "有没有新输入"这道判定**在接口层**（server 的 new_input）：只有那里同时
    看得到用户提交的原文与审计里的原问题，分得出"改写过"与"原样重发"。
    """
    from . import agent as _agent
    from .graph import precheck_resume

    g = ensure_graph(cfg)
    snap = g.get_state({"configurable": {"thread_id": thread_id}})
    extra = (clarification or "").strip()

    if not snap.values or not snap.next:
        # 没有活现场。给不出问题文本就返回 None（接口层照旧 404）。
        if not question.strip():
            return None
        return _agent.run_agent(question.strip(), cfg, org_id=org_id,
                                executor=executor, llm=llm,
                                thread_id=thread_id, clarification=extra,
                                handoff=handoff)

    values = snap.values or {}
    org = int(values.get("org_id", org_id if org_id is not None else 0))
    q = str(values.get("question") or question or "")
    trace_id = uuid.uuid4().hex[:12]
    tracer = Tracer()

    # 恢复前重新校验。中断与续跑之间隔着任意长的时间，检查点里存的是**中断
    # 那一刻**的前提；不重验就是拿旧前提接着跑。
    own_exec = executor is None
    ex = executor or Executor(cfg)
    try:
        block = precheck_resume(cfg, values, ex)
    finally:
        if own_exec and block is not None:
            ex.close()
    if block is not None:
        from .agent import _result

        tracer.add("resume_precheck", tracer.start(), block.error, status="blocked")
        r = _result(cfg, q, trace_id, thread_id, org, tracer, ok=False,
                    rejected_by=block.code, error=block.error, hint=block.hint)
        _write_resume_audit(cfg, r)
        return r

    # 补充的条件写回检查点**再续** —— 恢复是从图内部继续的，中间节点拿不到
    # resume() 的入参，只看得到状态。
    if extra:
        history = list(values.get("history") or [])
        history.append({"tool": "(发起人补充)", "args": {},
                        "brief": f"发起人补充的条件 —— 优先按它确定时间范围、"
                                 f"口径与统计维度：{extra}"})
        try:
            g.update_state({"configurable": {"thread_id": thread_id}},
                           {"history": history})
        except Exception:             # noqa: BLE001
            pass                      # 写不进去照样能续，那是原有语义

    client = llm or LlmClient(cfg)
    deps = Deps(cfg=cfg, llm=client, executor=ex, tracer=tracer,
                ctx=tools.ToolContext(cfg=cfg, org_id=org, executor=ex),
                # 续跑同样要能交接：它恢复的本来就是一条已经证明自己跑得久的
                # 线程，而这条路不经过入口等待 —— 节点边界是它唯一的交接点。
                handoff=handoff)
    try:
        final = g.invoke(None, {
            "configurable": {"thread_id": thread_id, "deps": deps},
            "recursion_limit": recursion_limit(int(values.get("max_steps", 6)))})
    finally:
        if own_exec:
            ex.close()
    final = {**final, "trace_id": trace_id}
    r = to_result(final, cfg, tracer)
    _write_resume_audit(cfg, r)
    return r


def _write_resume_audit(cfg: Config, result) -> None:
    """续跑也要如实写审计 —— 任务中心、复核队列、复放都从审计派生。
    审计不该成为查询失败的原因，所以吞异常（与 run_agent 同一处理）。"""
    from .graph import _audit_of
    from .trace import write_audit

    try:
        write_audit(cfg, _audit_of(result, cfg, "resume"))
    except Exception:                 # noqa: BLE001
        pass
