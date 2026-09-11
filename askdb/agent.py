"""可信数据 Agent —— LLM 自主决策循环（v2 设计 §1）。

与固定管道（graph.ask）并存的**另一条链路**，由配置 agent.enabled 门控。区别只在
"编排"：这里 LLM 每轮自己决定"下一步用哪个工具、传什么参数"，而不是走死的
schema_recall→generate→execute。安全性不变 —— 每次 execute_sql 仍是 tools.py 里那个
过 guard/dry_run/只读/脱敏 的安全原子，LLM 决定发哪条 SQL、绝不决定它能不能执行。

一次查询：
  1) grounded 召回      先 search_schema，让后面的判断有 schema 可依
  2) 意图 / 可答性预检   便宜一次结构化调用：可答？越域？单步/多步？（接地，不盲判）
  3) 自主循环          决策(选工具) → 调用 → 观察结果 → 再决策 …… 直到 finish
                       受 R-16 步数上限、R-17 累计 token 上限封顶，超则收敛作答并标注
  4) 收尾             用最后一次成功 execute_sql 的结果 + 归因结论打包成 AskResult

预算、脱敏、租户、可信信号都沿用既有实现：execute_sql 已把行脱敏、按 org 过滤，
循环全程只看得到过闸后的数据（挡设计 §10.1 的多步累积泄露）。
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from . import planner, skill, tools
from .audit import PHASE_STARTED
from .config import Config
from .executor import Executor
from .graph import AskResult, _audit_of
from .llm import LlmClient, LlmUsage
from .quota import QuotaExceeded, build_quota
from .trace import Tracer, now_iso, write_audit


# --------------------------------------------------------------------------
# 结构化产出
# --------------------------------------------------------------------------
class IntentCheck(BaseModel):
    """意图 / 可答性预检的结构化产出（接地：已看过召回的 schema）。"""

    answerable: bool = Field(description="用当前可用的表能不能回答这个问题")
    out_of_scope: bool = Field(
        default=False,
        description="问题涉及的业务实体在库里根本不存在（如问『供应商』但无供应商表）。"
                    "严禁攀附同名列硬答 —— 这种情况填 true。")
    clarify: str = Field(default="", description="answerable=false 且非越域时，说明缺什么、要澄清什么")
    multi_step: bool = Field(default=False, description="是否需要多步（先探分布/结果驱动分支）")
    reason: str = Field(default="", description="一句话判断依据")


class AgentAction(BaseModel):
    """自主循环每一轮的决策。"""

    thought: str = Field(default="", description="一句话：这一步为什么这么做")
    finish: bool = Field(default=False, description="证据已足以回答 → true；否则 false 并给出下一步工具")
    answer: str = Field(default="", description="finish=true 时的最终结论（含口径与归因）")
    tool: str = Field(default="", description="finish=false 时选的工具名")
    args: dict[str, Any] = Field(default_factory=dict, description="该工具的参数")


INTENT_SYSTEM = """你是数据查询的意图预检。已给你「可用的表与业务口径」，据此判断三件事：
1. answerable：用这些表能不能回答用户问题；
2. out_of_scope：问题里的业务实体是否在库中根本不存在——若不存在，**必须**判 true，
   严禁把它攀附到某个名字相近的列上硬answer（例如库里没有「供应商」实体，就不能拿
   model_config.vendor 之类同名列冒充）；
3. multi_step：是否需要多步（先看取值分布、或第一步结果决定第二步查哪张表）。
可答就 answerable=true；缺查询对象（纯指代、没主语）answerable=false 并在 clarify 写清缺什么。"""

INTENT_USER = """{schema}

【用户问题】
{question}"""

AGENT_SYSTEM = """你是一个可信查数 Agent。你不能直接写库，只能通过工具查数据。

可用工具（只读，可任意多次组合）：
{tools}

规则：
- 拿不准某张表的确切列名 / 枚举取值时，先用 get_table_schema 查清楚，**不要猜列名**。
- 口径要遵循「可用的表与业务口径」里的说明（例如某列的口径注释、枚举取值）。
- execute_sql 只写只读 SQL（SELECT/CTE）；它会自动过护栏、干跑、只读执行、脱敏。
- 一步只做一件事。看到工具结果后再决定下一步。
- 证据已经足以回答用户问题时，finish=true 并在 answer 写出结论——**结论要说明口径**，
  并且只基于工具真正返回的数据，不得编造。
- 若发现问题无法用现有表回答，也 finish=true，在 answer 如实说明无法回答及原因。"""

AGENT_USER = """{schema}

【用户问题】
{question}

【已完成的工具调用与结果】
{history}

【预算】剩余步数 {steps_left}。请给出下一步（选工具）或 finish=true 给出结论。"""


# --------------------------------------------------------------------------
def _brief(res: tools.ToolResult) -> str:
    """一条工具结果的简短摘要，进 trace。"""
    if not res.ok:
        return f"{res.tool} 未通过：{res.rejected_by or ''} {res.error}".strip()
    d = res.data
    if res.tool == "search_schema":
        return f"召回 {len(d.get('tables', []))} 张表" + ("（盲选）" if d.get("blind") else "")
    if res.tool == "get_table_schema":
        return f"{d.get('table')}：{len(d.get('columns', []))} 列"
    if res.tool == "execute_sql":
        return f"返回 {d.get('row_count', 0)} 行" + ("（有脱敏）" if d.get("masked_columns") else "")
    return "ok"


def _render_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return "（无）"
    out = []
    for i, h in enumerate(history, 1):
        line = f"第 {i} 步 · {h['tool']}({_fmt_args(h['args'])}) → {h['brief']}"
        if h.get("preview"):
            cols = "、".join(h["preview"]["columns"])
            rows = "；".join(", ".join(str(v) for v in r) for r in h["preview"]["rows"])
            line += f"\n    列：{cols}\n    前几行：{rows}"
        elif h.get("columns"):
            line += f"\n    列：{'、'.join(h['columns'])}"
        out.append(line)
    return "\n".join(out)


def _fmt_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in (args or {}).items():
        s = str(v)
        parts.append(f"{k}={s[:60]}")
    return ", ".join(parts)


def _render_specs() -> str:
    """把只读工具规格渲染成可读列表，注入 AGENT_SYSTEM。"""
    lines = []
    for s in tools.tool_specs():
        params = "，".join(f"{k}（{v}）" for k, v in s["params"].items())
        lines.append(f"- {s['name']}：{s['summary']}。参数：{params}")
    return "\n".join(lines)


def _sys(base: str, cfg: Config) -> str:
    """系统提示 = 基底 + Skill 方法论口径（可信的来源，见 skill.py）。"""
    block = skill.render(cfg)
    return f"{base}\n\n{block}" if block else base


def _budget(cfg: Config) -> tuple[int, int]:
    a = cfg.raw.get("agent", {}) or {}
    pl = cfg.raw.get("planner", {}) or {}
    max_steps = int(a.get("max_steps", 6))                                  # R-16
    cost_cap = int(a.get("cost_cap_tokens", pl.get("cost_cap_tokens", 12000)))  # R-17
    return max_steps, cost_cap


def _result(cfg: Config, question: str, trace_id: str, thread_id: str, org: int,
            tracer: Tracer, *, ok: bool, reasoning: str = "", last_exec: dict | None = None,
            rejected_by: str | None = None, error: str = "", hint: str = "",
            tables_hit: list[str] | None = None, step_count: int = 1,
            converged: str = "") -> AskResult:
    d = last_exec or {}
    return AskResult(
        ok=ok, question=question, trace_id=trace_id, org_id=org, thread_id=thread_id,
        sql_final=d.get("sql_final", ""), reasoning=reasoning,
        rules_fired=list(d.get("rules_fired", [])), rewrites=list(d.get("rewrites", [])),
        columns=list(d.get("columns", [])), rows=list(d.get("rows", [])),
        row_count=int(d.get("row_count", 0)), truncated=bool(d.get("truncated", False)),
        as_of=d.get("as_of", ""), explain_rows=d.get("explain_rows"),
        masked_columns=list(d.get("masked_columns", [])),
        mask_degraded=bool(d.get("mask_degraded", False)),
        rejected_by=rejected_by, error=error, hint=hint,
        tables_hit=list(tables_hit or []),
        multi_step=step_count > 1, step_count=step_count, converged_early=converged,
        steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        tok_in=tracer.tok_in, tok_out=tracer.tok_out, cost_cny=tracer.cost_cny,
    )


def run_agent(question: str, cfg: Config, org_id: int | None = None, *,
              executor: Executor | None = None, llm: LlmClient | None = None,
              trace_id: str | None = None, thread_id: str | None = None) -> AskResult:
    """自主决策循环入口。返回与 graph.ask 同一套 AskResult，并写审计（收尾）。

    审计是任务中心 / 复核队列 / replay 的共同数据源：它们都从审计记录派生
    （audit.tasks / audit.needs_review），所以 agent 链路一旦如实写审计，这三样
    立刻复用现网机制，无需各造一套。
    """
    result = _drive(question, cfg, org_id, executor=executor, llm=llm,
                    trace_id=trace_id, thread_id=thread_id)
    try:                                  # 审计不该成为查询失败的原因
        write_audit(cfg, _audit_of(result, cfg, "ask"))
    except Exception:
        pass
    return result


def _drive(question: str, cfg: Config, org_id: int | None = None, *,
           executor: Executor | None = None, llm: LlmClient | None = None,
           trace_id: str | None = None, thread_id: str | None = None) -> AskResult:
    trace_id = trace_id or uuid.uuid4().hex[:12]
    thread_id = thread_id or trace_id
    org = org_id if org_id is not None else int(cfg.raw.get("tenant", {}).get("default_ctx", 0) or 0)
    tracer = Tracer()

    # 每日配额快速失败（与管道同一口径）。
    dq = build_quota(cfg)
    over, used = dq.exhausted()
    if over:
        tracer.add("quota", tracer.start(), f"当日已用 {used}/{dq.limit}", status="blocked")
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="QUOTA", error=f"已达当日模型调用上限（{used}/{dq.limit}）",
                       hint="明日自动恢复；直查 SQL 不受配额限制。")

    # 发起记录先落盘：进程中途被杀时检查点/审计里仍有这条线程，任务中心据此
    # 列得出、凭 thread_id 续得上（与 _execute 同一处理）。它只带"这条线程存在、
    # 归谁、打哪个库、问的什么"，收尾记录到时共用 trace_id 顶掉它。
    try:
        write_audit(cfg, {
            "trace_id": trace_id, "ts": now_iso(), "kind": "ask",
            "phase": PHASE_STARTED, "thread_id": thread_id, "org_id": org,
            "question": question, "role": cfg.role or "ANONYMOUS", "user": cfg.user or "",
            "source": cfg.source_id or "builtin", "source_name": cfg.source_name or cfg.path,
        })
    except Exception:
        pass

    ex = executor or Executor(cfg)
    client = llm or LlmClient(cfg)
    ctx = tools.ToolContext(cfg=cfg, org_id=org, executor=ex)
    max_steps, cost_cap = _budget(cfg)

    # 1) grounded 召回
    t = tracer.start()
    rec = tools.search_schema(question, cfg)
    tables_hit = rec.data.get("tables", [])
    schema_prompt = rec.data.get("prompt", "")
    tracer.add("schema_recall", t, _brief(rec), tables=tables_hit)

    # 2) 意图 / 可答性预检
    t = tracer.start()
    try:
        intent, u = client.structured(
            IntentCheck, _sys(INTENT_SYSTEM, cfg),
            INTENT_USER.format(schema=schema_prompt, question=question))
    except QuotaExceeded as e:
        tracer.add("intent", t, str(e), status="blocked")
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="QUOTA", error=str(e), hint="明日自动恢复。")
    except Exception as e:
        tracer.add("intent", t, f"预检失败：{e}", status="failed")
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="LLM", error=f"意图预检失败：{e}",
                       hint="检查网络与密钥；也可关闭 agent.enabled 退回管道。")
    tracer.add("intent", t, intent.reason, model=client.model_name,
               tok_in=u.input_tokens, tok_out=u.output_tokens, cost_cny=u.cost_cny)

    if intent.out_of_scope:
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="OOS", tables_hit=tables_hit,
                       error=intent.reason or "该问题涉及的业务实体在当前库中不存在，无法回答。",
                       reasoning=intent.reason)
    if not intent.answerable:
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="CLARIFY", tables_hit=tables_hit,
                       error=intent.clarify or "问题缺少明确的查询对象，请补充。",
                       reasoning=intent.clarify)

    # 3) 自主循环
    agent_system = _sys(AGENT_SYSTEM.format(tools=_render_specs()), cfg)
    history: list[dict[str, Any]] = []
    last_exec: dict | None = None
    answer = ""
    converged = ""
    step_count = 0
    for step in range(1, max_steps + 1):
        if tracer.tok_in + tracer.tok_out > cost_cap:                       # R-17
            converged = f"累计 token 超预算 {cost_cap}，收敛作答"
            break
        human = AGENT_USER.format(
            schema=schema_prompt, question=question,
            history=_render_history(history), steps_left=max_steps - step + 1)
        t = tracer.start()
        try:
            action, u = client.structured(AgentAction, agent_system, human)
        except QuotaExceeded as e:
            tracer.add("decide", t, str(e), status="blocked")
            converged = "配额耗尽，收敛"
            break
        except Exception as e:
            tracer.add("decide", t, f"决策失败：{e}", status="failed")
            return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                           rejected_by="LLM", error=f"决策失败：{e}", tables_hit=tables_hit,
                           step_count=max(1, step_count))
        tracer.add("decide", t, (action.thought or "")[:80], model=client.model_name,
                   tok_in=u.input_tokens, tok_out=u.output_tokens, cost_cny=u.cost_cny)

        if action.finish:
            answer = action.answer
            break

        step_count += 1
        res = tools.invoke(action.tool, action.args, ctx)
        tt = tracer.start()
        # 静态 step 名（工具名进 note）：复放/追踪页的步骤映射是静态表，
        # 动态 step id 会显示成原始串（见 tests/test_frontend）。
        tracer.add("tool_call", tt, f"{action.tool}·{_brief(res)}",
                   status="ok" if res.ok else "blocked")

        # 高成本查询 → 挂起人工审批（HITL）。把 R-11 顶到结果层，交由 server
        # 既有 _open_approval 建审批单：等审批 = 一次无界等待，正是任务/异步的
        # 落点（设计 §2/§3）。带上被拦的 SQL 与预估扫描量，审批人才看得到差在哪。
        if res.rejected_by == "R-11":
            return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                           rejected_by="R-11", last_exec=res.data,
                           error=res.error, tables_hit=tables_hit,
                           step_count=max(1, step_count))
        item: dict[str, Any] = {"tool": action.tool, "args": action.args, "brief": _brief(res)}
        if res.ok and action.tool == "execute_sql":
            last_exec = res.data
            ctx.last_result = res.data          # 供 analyze_result / export_result 用
            item["preview"] = {"columns": res.data.get("columns", []),
                               "rows": planner.preview_rows(res.data.get("rows", []))}
        elif res.ok and action.tool == "get_table_schema":
            item["columns"] = [c["name"] for c in res.data.get("columns", [])]
        elif res.ok and action.tool == "search_schema":
            item["columns"] = res.data.get("tables", [])
        history.append(item)
    else:
        converged = f"达步数上限 {max_steps}，收敛作答"

    # 4) 收尾
    if not answer and converged:
        answer = "（在预算内未完全收敛）" + converged
    ok = bool(last_exec) or bool(answer)
    if not ok:
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="NO_RESULT", error="未能产出结果", tables_hit=tables_hit,
                       step_count=max(1, step_count), converged=converged)
    return _result(cfg, question, trace_id, thread_id, org, tracer, ok=True,
                   reasoning=answer, last_exec=last_exec, tables_hit=tables_hit,
                   step_count=max(1, step_count), converged=converged)
