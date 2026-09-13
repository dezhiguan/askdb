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
    tables_hit: list[str]

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
    from .agent import _brief

    d = _deps(config)
    t = d.tracer.start()
    rec = tools.search_schema(state["question"], d.cfg)
    tables_hit = rec.data.get("tables", [])
    schema_prompt = rec.data.get("prompt", "")
    # 输出必须是**喂进提示词的表结构全文**：排查"模型为什么没用那张表"时，
    # 召回对了但结构没渲染出某一列，与压根没召回那张表，在"召回 N 张表"
    # 这句 note 上完全一样，只有全文分得开。
    d.tracer.add("schema_recall", t, _brief(rec), tables=tables_hit,
                 input=state["question"], output=schema_prompt)
    return {"tables_hit": tables_hit, "schema_prompt": schema_prompt}


def _n_intent(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """意图 / 可答性预检。超出这个库的范围就别开始烧 token。"""
    from .agent import INTENT_SYSTEM, INTENT_USER, IntentCheck, _sys

    d = _deps(config)
    t = d.tracer.start()
    try:
        intent, u = d.llm.structured(
            IntentCheck, _sys(INTENT_SYSTEM, d.cfg),
            INTENT_USER.format(schema=state.get("schema_prompt", ""),
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
        "tok_used": state.get("tok_used", 0) + u.input_tokens + u.output_tokens}
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


def _n_decide(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """模型自己挑下一步调哪个工具 —— 这一步是 agent 与老管道的**全部区别**。

    配额耗尽走 converged 而不是 rejected_by：已经查到的东西还在，该收敛作答，
    不是报错丢掉。
    """
    from .agent import (AGENT_SYSTEM, AGENT_USER, AgentAction, _render_history,
                        _render_specs, _sys)

    d = _deps(config)
    step = state.get("step", 0) + 1
    human = AGENT_USER.format(
        schema=state.get("schema_prompt", ""), question=state["question"],
        history=_render_history(state.get("history") or []),
        steps_left=max(0, _step_cap(state) - step + 1))
    t = d.tracer.start()
    try:
        action, u = d.llm.structured(
            AgentAction, _sys(AGENT_SYSTEM.format(tools=_render_specs()), d.cfg), human)
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
        # 决策结果进 state 供 _n_act 读。它是可序列化的普通 dict，不是
        # AgentAction 对象 —— 检查点存不下 pydantic 模型。
        # finish 写的是**归一之后**的值：_after_decide 读它来路由，两处各判
        # 一次就会出现"这里按工具走、那里按收尾走"。
        "action": {"finish": finish, "answer": action.answer or "",
                   "tool": action.tool or "", "args": dict(action.args or {})},
    }
    if finish:
        out["answer"] = action.answer or ""
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

    bad = grounding.ungrounded(answer, state.get("exec_results") or [],
                               known=_known_constants(d.cfg))
    if not bad:
        return {"ungrounded": []}

    ungrounded = [grounding.fmt([x]) for x in bad]
    d.tracer.add("grounding", d.tracer.start(),
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

    res = tools.invoke(tool_name, args, d.ctx)
    tt = d.tracer.start()
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
        out["exec_results"] = list(state.get("exec_results") or []) + [
            {"columns": list(res.data.get("columns") or []),
             "rows": list(res.data.get("rows") or [])}]
        # ④ 供 analyze_result / export_result 用。ctx 不进检查点，它是本次
        #    执行的现场；续跑时从 history 重建不了，那两个工具因此只在
        #    同一次执行内可用 —— 与改造前一致。
        d.ctx.last_result = res.data
        item["preview"] = {"columns": res.data.get("columns", []),
                           "rows": planner.preview_rows(res.data.get("rows", [])),
                           "row_count": res.data.get("row_count")}
    elif res.ok and tool_name == "get_table_schema":
        item["columns"] = [c["name"] for c in res.data.get("columns", [])]
    elif res.ok and tool_name == "search_schema":
        item["columns"] = res.data.get("tables", [])

    history.append(item)
    out["history"] = history
    _check_handoff(d, state, step=state.get("step", 0),
                   explain_rows=int(res.data.get("explain_rows") or 0)
                                if isinstance(res.data, dict) else 0)
    return out


def _meta_evidence(state: AgentState) -> list[dict[str, Any]]:
    """把**非 execute_sql** 的成功工具返回也折成可核对的证据。

    search_schema 返回表清单、get_table_schema 返回列清单 —— 这些是模型回答
    "库里有哪些表""某张表有哪些字段"时的真实依据，只是它们不经 execute_sql，
    exec_results 里一条都没有。不把它们算作证据，这类问题就只能被判成编造。

    除了名字本身，**还把清单长度一并给出**：模型说"共 8 张表"时，那个 8 正是
    len(tables)，它在返回值里不作为一个元素存在，只作为个数存在。
    """
    out: list[dict[str, Any]] = []
    for h in state.get("history") or []:
        if not h.get("ok") or h.get("tool") == "execute_sql":
            continue
        names = list(h.get("columns") or [])
        if not names:
            continue
        out.append({"columns": ["name"], "rows": [[n] for n in names]})
        out.append({"columns": ["count"], "rows": [[len(names)]]})
    return out


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
    from .agent import (_grounding_mode, _has_number, _io_json,
                        _known_constants)

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
        # 有数据。模型没来得及归因时，别用一句"未完全收敛"把已经查到的结果盖掉
        # —— 结果表就在 AskResult 里，直说"看表"比丢掉它诚实得多。
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
        if not ran_tool:
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
        bad = grounding.ungrounded(answer, _meta_evidence(state),
                                   known=_known_constants(d.cfg))
        if bad:
            return _verdict(
                f"没有一次 execute_sql 跑成，结论里 {len(bad)} 个数也追溯不到"
                f"任何一次工具返回，不给出这个答案",
                rejected_by="NO_EVIDENCE", answer="",
                error=f"本轮没有任何一次查询执行成功，而结论里这些数字追溯不到"
                      f"工具返回：{grounding.fmt(bad)}。" + tail,
                hint="换个更具体的问法，或先确认这个口径需要的表是否可查；"
                     "也可在「直查 SQL」里自己跑一条核对。")
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
                    ("decide", _n_decide), ("act", _n_act),
                    ("ground", _n_ground), ("finalize", _n_finalize)):
        g.add_node(name, fn)
    g.set_entry_point("recall")
    g.add_edge("recall", "intent")
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
        "schema_prompt": "", "tables_hit": [],
        "history": history, "exec_results": [],
        "last_exec": None, "scan_blocked": None, "last_error": "",
        "answer": "", "converged": "", "step": 0, "step_count": 0,
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
        last_exec=state.get("last_exec"),
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
