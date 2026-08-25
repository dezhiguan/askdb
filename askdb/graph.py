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

import sqlite3
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from . import guard, planner, schema_rag
from .config import Config
from .executor import DataSourceError, Executor
from .llm import LlmClient, LlmNotConfigured
from .quota import QuotaExceeded, build_quota
from .trace import Tracer, cost_cny, now_iso, write_audit


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

    sql_raw: str
    sql_final: str
    reasoning: str
    rules_fired: list[str]
    rewrites: list[str]

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    as_of: str            # 数据时间，来自数据源时钟（§8 准入条件 #7）
    explain_rows: int | None

    error: str | None
    error_hint: str
    rejected_by: str | None
    attempt: int
    # 本次拒绝是否属「问题超出范围」。路由据此决定要不要进反思。
    out_of_scope: bool
    # 执行报错是否可由重试救回（超时可以，连接不可达不行）
    exec_retryable: bool
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

    rejected_by: str | None = None
    error: str = ""
    hint: str = ""

    tables_hit: list[str] = field(default_factory=list)
    metrics_hit: list[str] = field(default_factory=list)
    attempts: int = 1
    step_count: int = 1
    multi_step: bool = False
    sub_steps: list[dict[str, Any]] = field(default_factory=list)
    converged_early: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
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
# 节点
# --------------------------------------------------------------------------

def _n_retrieve(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    r = schema_rag.recall(state["question"], d.cfg)
    note = f"命中 {len(r.tables)} 张表（白名单共 {len(d.cfg.tables)} 张）"
    if r.metrics:
        note += f"；命中口径 {'、'.join(m.name for m in r.metrics)}"
    if r.truncated:
        note += f"；因 token 预算裁掉 {'、'.join(r.truncated)}"
    if r.note:
        note += f"；{r.note}"
    d.tracer.add("schema_recall", t, note)
    return {
        "schema_prompt": r.prompt,
        "tables_hit": r.table_names,
        "metrics_hit": [m.name for m in r.metrics],
        "recall_truncated": r.truncated,
    }


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
                                         question=state["question"]))
        else:
            plan, usage = d.llm.structured(
                planner.Plan, planner.REPLAN_SYSTEM,
                planner.REPLAN_USER.format(
                    schema=state["schema_prompt"], question=state["question"],
                    history=planner.render_history(state.get("steps_done") or []),
                    carry=planner.render_carry(state.get("carry") or {})))
    except QuotaExceeded as e:
        d.tracer.add("plan", t, str(e), status="blocked")
        return {"error": str(e), "error_hint": "明日自动恢复。直查 SQL 不受配额限制。",
                "rejected_by": "QUOTA"}
    except Exception as e:
        d.tracer.add("plan", t, f"规划失败：{e}", status="failed")
        return {"error": f"规划失败：{e}",
                "error_hint": "检查网络与密钥；也可关闭 planner.enabled 退回单步。",
                "rejected_by": "LLM"}

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
                         tok_in=usage.input_tokens, tok_out=usage.output_tokens)
            # 不要动 step_no —— 它由 assess 递增，这里再加一次就成了双重递增
            return {"goal": fallback, "multi_step": True,
                    "attempt": 0, "sql_raw": "", "error": None, "enough": False}
        # 连 assess 都没说清缺什么 —— 此时继续下去也是空转，如实收敛并标注
        d.tracer.add("plan", t, "重规划与结果评估均未给出下一步，收敛作答",
                     status="failed",
                     tok_in=usage.input_tokens, tok_out=usage.output_tokens)
        return {"enough": True, "goal": "",
                "converged_early": "结果评估判定不足，但未能给出下一步目标"}

    if first:
        note = ("判定需多步：" + plan.reason) if plan.multi_step else ("判定单步可答：" + plan.reason)
    else:
        note = f"第 {step_no + 1} 步目标：{plan.goal}"
    d.tracer.add("plan", t, note, tok_in=usage.input_tokens, tok_out=usage.output_tokens)
    return {"multi_step": bool(plan.multi_step) if first else state.get("multi_step", False),
            "goal": plan.goal or "", "enough": False}


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
        draft, usage = d.llm.generate_sql(
            question=state["question"],
            schema_prompt=state["schema_prompt"],
            last_sql=state.get("sql_raw", ""),
            error=state.get("error") or "",
            step=step_ctx,
        )
    except LlmNotConfigured as e:
        d.tracer.add("generate_sql", t, "未配置模型密钥", status="failed")
        return {"error": str(e), "error_hint": "配置密钥后重试", "rejected_by": "LLM"}
    except QuotaExceeded as e:
        d.tracer.add("generate_sql", t, str(e), status="blocked")
        return {"error": str(e), "error_hint": "明日自动恢复。直查 SQL 不受配额限制。",
                "rejected_by": "QUOTA"}
    except Exception as e:
        d.tracer.add("generate_sql", t, f"模型调用失败：{e}", status="failed")
        return {
            "error": f"模型调用失败：{e}",
            "error_hint": "检查网络与密钥是否有效；也可稍后重试。",
            "rejected_by": "LLM",
        }

    label = "生成 1 条 SELECT" if attempt == 0 else f"第 {attempt + 1} 轮重新生成"
    d.tracer.add("generate_sql", t, label, tok_in=usage.input_tokens, tok_out=usage.output_tokens)
    if not (draft.sql or "").strip():
        return {
            "error": draft.reasoning or "模型判断当前表结构无法回答该问题。",
            "error_hint": "换个问法，或在 config/tables.yaml 中开放更多表。",
            "rejected_by": "NO_SQL",
            "reasoning": draft.reasoning,
        }
    return {"sql_raw": draft.sql, "reasoning": draft.reasoning, "error": None, "rejected_by": None}


def _n_guard(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    r = guard.check(state["sql_raw"], d.cfg, org_id=state["org_id"], dialect=d.cfg.dialect)
    if not r.ok:
        d.tracer.add("guard", t, f"{r.rejected_by} {r.reason}", status="blocked")
        return {"error": r.reason, "rejected_by": r.rejected_by,
                # 超范围的拒绝不进反思。路由只读状态，判定在这里定死。
                "out_of_scope": r.out_of_scope,
                "error_hint": ("该对象不在开放范围内。可在接入页查看已开放的表，"
                               "或联系管理员调整白名单。") if r.out_of_scope else ""}

    note = "；".join(r.rewrites) or "无需改写"
    d.tracer.add("guard", t, note)
    return {
        "sql_final": r.sql, "rules_fired": r.rules_fired, "rewrites": r.rewrites,
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
    if not r.ok:
        d.tracer.add("dry_run", t, r.reason, status="blocked")
        return {
            "error": r.reason,
            "error_hint": "缩小时间范围或加筛选条件，让扫描量降下来。",
            "rejected_by": "R-11",
        }
    est = f"预估扫描 {r.est_rows:,} 行" if r.est_rows is not None else "计划无基数估计"
    d.tracer.add("dry_run", t, est)
    return {"error": None, "rejected_by": None, "explain_rows": r.est_rows}


def _n_execute(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    try:
        d.executor.set_org(state["org_id"])   # RLS 兜底层读这个上下文
        res = d.executor.run(state["sql_final"])
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
    d.tracer.add("execute", t, note)
    return {
        "columns": [str(c) for c in res.columns],
        "rows": [[jsonable(v) for v in row] for row in res.rows],
        "row_count": res.row_count, "truncated": res.truncated,
        "as_of": res.as_of,
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
    used = d.tracer.tok_in + d.tracer.tok_out
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
        d.tracer.add("assess", t, str(e), status="blocked")
        return {**base, "enough": True, "converged_early": str(e)}
    except Exception as e:
        d.tracer.add("assess", t, f"评估失败，按足够处理：{e}", status="failed")
        return {**base, "enough": True}

    if a.enough:
        d.tracer.add("assess", t, f"足以作答 ✓ {a.reason}",
                     tok_in=usage.input_tokens, tok_out=usage.output_tokens)
        return {**base, "enough": True, "carry": {}}

    ok, why = planner.carry_within_limit(a.carry, d.cfg)
    if not ok:
        # R-15：下传规模超限往往说明上一步筛选本身有问题
        d.tracer.add("assess", t, f"{why}，收敛作答（R-15）", status="blocked",
                     tok_in=usage.input_tokens, tok_out=usage.output_tokens)
        return {**base, "enough": True, "converged_early": why}

    carried = "、".join(f"{k}={v}" for k, v in a.carry.items()) or "无"
    d.tracer.add("assess", t, f"不足以作答 → 重规划（第 {step_no}/{state.get('max_steps')} 步）"
                              f"；下传 {carried}",
                 status="failed", tok_in=usage.input_tokens, tok_out=usage.output_tokens)
    return {**base, "enough": False, "carry": a.carry,
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


def _n_finalize(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    d.tracer.add("finalize", t, "已附最终 SQL 与判定链路")
    return {}


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
    g.add_node("retrieve", _n_retrieve)
    g.add_node("plan", _n_plan)
    g.add_node("assess", _n_assess)
    g.add_node("generate", _n_generate)
    g.add_node("guard", _n_guard)
    g.add_node("dry_run", _n_dry_run)
    g.add_node("execute", _n_execute)
    g.add_node("reflect", _n_reflect)
    g.add_node("finalize", _n_finalize)

    g.set_entry_point("retrieve")
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


def build_graph(checkpoint_db: Path | None = None):
    """编译状态机。

    接检查点的目的是**失败样本可原样复现**（技术设计说明书 §5），
    用于 P3 评测归因，不是在线断点续跑。
    """
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


_GRAPH = None
_GRAPH_KEY: str | None = None


def replay(trace_id: str, cfg: Config) -> list[dict[str, Any]]:
    """取回某次调用的全部检查点快照，用于失败复现与归因（P3）。"""
    g = build_graph(cfg.checkpoint_db)
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


def ask(
    question: str,
    cfg: Config,
    org_id: int | None = None,
    executor: Executor | None = None,
    llm: LlmClient | None = None,
) -> AskResult:
    """跑一次完整链路。executor / llm 可注入，便于测试与复用连接。"""
    global _GRAPH, _GRAPH_KEY
    key = str(cfg.checkpoint_db)
    if _GRAPH is None or _GRAPH_KEY != key:
        _GRAPH = build_graph(cfg.checkpoint_db)
        _GRAPH_KEY = key

    org = cfg.default_org if org_id is None else org_id
    trace_id = uuid.uuid4().hex[:12]
    tracer = Tracer()

    # 每日配额（技术设计说明书 §8 准入条件第 6 条）。
    #
    # 这里只是**快速失败**：额度早已用尽时不必把整条链路跑到模型那一步，
    # 直接给出明确结论。真正的扣减在 LlmClient 里，一次调用扣一次 ——
    # 一次提问会调好几次模型，在入口按请求扣会低估花费好几倍。
    #
    # 只读探测拿到的用量是瞬时值，读完到真正调用之间还会有并发变化，
    # 所以这一层不能算把关；把关靠 LlmClient 的原子预扣。
    dq = build_quota(cfg)
    over, used = dq.exhausted()
    if over:
        tracer.add("quota", tracer.start(), f"当日已用 {used}/{dq.limit}", status="blocked")
        # 拦截也留痕：配额挡下的调用同样要进流水 —— 审计页上"被挡了多少"
        # 与"放行了多少"同等重要，缺一半就对不上账。
        write_audit(cfg.audit_log, {
            "trace_id": trace_id, "ts": now_iso(), "kind": "ask",
            "model": cfg.llm.get("model"),
            "org_id": org, "question": question,
            "tables_hit": [], "metrics_hit": [], "sql_raw": "", "sql_final": "",
            "rules_fired": [], "rejected_by": "QUOTA", "attempts": 0,
            "explain_rows": None, "step_count": 0, "multi_step": False,
            "converged_early": "", "rows_returned": 0,
            "elapsed_ms": tracer.elapsed_ms, "tok_in": 0, "tok_out": 0,
            "cost_cny": 0.0, "steps": tracer.as_list(),
        })
        return AskResult(
            ok=False, question=question, trace_id=trace_id, org_id=org,
            rejected_by="QUOTA",
            error=f"已达当日模型调用上限（{used}/{dq.limit}）",
            hint="明日自动恢复；也可调高配置中的 observability.daily_quota。",
            steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        )

    own_exec = executor is None
    ex = executor or Executor(cfg)
    deps = Deps(cfg=cfg, llm=llm or LlmClient(cfg), executor=ex, tracer=tracer)

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
    }
    try:
        out = _GRAPH.invoke(
            init,
            {
                "recursion_limit": 40,
                "configurable": {"thread_id": trace_id, "deps": deps},
                # LangSmith 接线：run 树以 metadata.trace_id 与本地审计互相
                # 定位。未开启 tracing 时这两项只是无人消费的元数据，零开销。
                "run_name": "askdb.ask",
                "metadata": {"trace_id": trace_id, "org_id": org, "kind": "ask"},
            },
        )
    finally:
        if own_exec:
            ex.close()

    tok_in, tok_out = tracer.tok_in, tracer.tok_out
    result = AskResult(
        ok=not out.get("error") and bool(out.get("sql_final")),
        question=question, trace_id=trace_id, org_id=org,
        sql_raw=out.get("sql_raw", ""), sql_final=out.get("sql_final", ""),
        reasoning=out.get("reasoning", ""),
        rules_fired=list(out.get("rules_fired") or []),
        rewrites=list(out.get("rewrites") or []),
        columns=out.get("columns", []), rows=out.get("rows", []),
        row_count=out.get("row_count", 0), truncated=out.get("truncated", False),
        as_of=out.get("as_of", ""),
        rejected_by=out.get("rejected_by"), error=out.get("error") or "",
        hint=out.get("error_hint", ""),
        tables_hit=out.get("tables_hit", []), metrics_hit=out.get("metrics_hit", []),
        attempts=out.get("attempt", 0) + 1,
        step_count=max(out.get("step_no", 1), 1),
        multi_step=bool(out.get("multi_step", False)),
        sub_steps=list(out.get("steps_done") or []),
        converged_early=out.get("converged_early", ""),
        steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        tok_in=tok_in, tok_out=tok_out,
        cost_cny=cost_cny(tok_in, tok_out, cfg.llm),
    )

    write_audit(cfg.audit_log, {
        "trace_id": trace_id, "ts": now_iso(), "kind": "ask",
        "model": cfg.llm.get("model"),
        "org_id": org, "question": question,
        "tables_hit": result.tables_hit, "metrics_hit": result.metrics_hit,
        "sql_raw": result.sql_raw, "sql_final": result.sql_final,
        "rules_fired": result.rules_fired, "rejected_by": result.rejected_by,
        "attempts": result.attempts, "explain_rows": out.get("explain_rows"),
        "step_count": result.step_count, "multi_step": result.multi_step,
        "converged_early": result.converged_early,
        "rows_returned": result.row_count,
        "elapsed_ms": result.elapsed_ms, "tok_in": tok_in, "tok_out": tok_out,
        "cost_cny": result.cost_cny, "steps": result.steps,
    })
    return result
