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

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field

from . import grounding, planner, skill, tools
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
   model_config.vendor 之类同名列冒充）。
   判据是**有没有承载这个实体的表**，不是"有没有名字像的列"：一个属性列
   （vendor / type / category / source / ref_type）哪怕名字完全对上，也不等于
   那个实体存在。实测反复出现的错法是——先在口径里写明"表中没有独立的 X 实体表"，
   然后照样拿同名列数出一个数交差；**写得出这句话就说明该判 true**。
   同一个问题换个问法（"我们有多少 X"／"X 的数量是多少"／"统计一下 X 总数"）
   判定必须一致，不能因为措辞不同就一会儿拒答一会儿攀附；
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
- 若发现问题无法用现有表回答，也 finish=true，在 answer 如实说明无法回答及原因。

结论里的数字，以下五条是硬约束（违反即为错答，且事后可核）：
1. **没跑过就不许写。** 结论里出现的每一个数字，都必须是某次 execute_sql 真正返回过的值，
   或由这些值直接算得。一次 execute_sql 都没成功时，不许写任何数字，也不许说"经查证"
   "已用 DISTINCT 核实""三张表互相印证"这类话——没查就是没查。
2. **结果集行数 ≠ 表的行数。** 一条带 WHERE / LIMIT 的 SQL 返回 N 行，只说明"符合这些条件的
   有 N 行"。要陈述某张表一共多少行，必须单独跑一次不带过滤的 COUNT。
3. **被截断的结果不能用来说总量。** 工具结果标注"已被 R-13 截断"时，那不是全部行；
   此时禁止给出总数、总和、覆盖范围一类的总量性表述，只能描述已看到的部分并说明它被截断了。
4. **列要按它在 SQL 里的真实含义命名。** 你写的是 COUNT(*) 就不能在结论里把它叫成
   "已排除软删除的条数"；写的是 COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) 就要叫
   "已删除数"。要"排除软删除后的条数"，就在 SQL 里真的写 WHERE deleted_at IS NULL。
5. **比率、百分比、差值一律在 SQL 里算成一列，不要在结论里心算。** 需要 a/b 就写
   ROUND(100.0 * a / NULLIF(b, 0), 4) AS xxx_pct，让库算完再读。

6. **看到的只是前几行，不代表整列都长这样。** 工具标注"仅前 N 行"时，禁止据此断言
   "全部都是……""共有 N 家"。要判断整列的分布或总量，另跑一条 GROUP BY /
   COUNT(*) FILTER，别拿可见的那几行外推。
7. **合计也别自己加。** 需要总数就在同一条 SQL 里多选一列 COUNT(*) / SUM(...)，
   或单独再查一次。手动把分项加起来实测会加错（把五个正确的分项加成了
   1,008,010，真值 1,027,010），而分项全对会让这个错的合计显得很可信。

另外：表名、列名、枚举取值一律**逐字照抄**工具返回的原值，不得改写成同义词或翻译
（例如返回的是 ANSWER 就不能写成 CHAT）；需要解释时在括号里补中文说明。"""

AGENT_USER = """{schema}

【用户问题】
{question}

【已完成的工具调用与结果】
{history}

【预算】剩余步数 {steps_left}。请给出下一步（选工具）或 finish=true 给出结论。"""


# --------------------------------------------------------------------------
def _io_json(obj: object) -> str:
    """把工具的参数/返回折成一段可读 JSON，进 span 的输入/输出。

    **取不到就退回 str()，绝不抛**：观测字段把主链路弄崩是最坏的结果。
    截断由 tracer.add 统一做（见 trace.clip_io），这里不重复一套上限。
    """
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except Exception:
        return str(obj)


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
        s = f"返回 {d.get('row_count', 0)} 行"
        if d.get("truncated"):
            # 截断这件事必须在模型看得见的地方说清楚。只在响应里置 truncated=true
            # 而历史里只写"返回 200 行"，模型就会把 200 当成全量——2026-09-11 跑测
            # 里的「全库共 200 条评分记录」正是这么来的。
            s += "（**已被 R-13 截断，这不是全部行**，不得据此陈述总量）"
        if d.get("masked_columns"):
            s += "（有脱敏）"
        return s
    return "ok"


def _render_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return "（无）"
    out = []
    for i, h in enumerate(history, 1):
        line = f"第 {i} 步 · {h['tool']}({_fmt_args(h['args'])}) → {h['brief']}"
        if h.get("preview"):
            cols = "、".join(h["preview"]["columns"])
            pv = h["preview"]["rows"]
            rows = "；".join(", ".join(str(v) for v in r) for r in pv)
            total = h["preview"].get("row_count")
            # 必须把"你只看到了几行、一共几行"写死在这里。只写"前几行"太轻，
            # 模型会拿看得见的那几行去断言整列：2026-09-11 复测里它按
            # is_active DESC 排序取回 18 行，看到开头全是 true 就说"18 家全部在用"
            # （实际 13 家）。
            head = (f"仅前 {len(pv)} 行（本次共返回 {total} 行，**其余未展示，"
                    f"不得据此断言整列的分布**）"
                    if isinstance(total, int) and total > len(pv) else "全部行")
            line += f"\n    列：{cols}\n    {head}：{rows}"
        elif h.get("columns"):
            line += f"\n    列：{'、'.join(h['columns'])}"
        out.append(line)
    return "\n".join(out)


#: 各参数在历史里保留多长。sql 单列一档：模型要靠回看自己发过的 SQL 才能
#: 说准口径（"这一列到底是 COUNT(*) 还是 COUNT(*) FILTER(...)"），截到 60 字
#: 等于把 SELECT 列表整段切掉——2026-09-11 跑测里把 COUNT(*) 说成"已排除软删除
#: 的条数"，就是看不见自己写了什么。
_ARG_KEEP = {"sql": 800}
_ARG_KEEP_DEFAULT = 60


def _fmt_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in (args or {}).items():
        s = str(v)
        keep = _ARG_KEEP.get(k, _ARG_KEEP_DEFAULT)
        if len(s) > keep:
            s = s[:keep] + " …（已截断）"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


#: 阿拉伯数字。判"这句话有没有在陈述一个量"，全角数字一并算上。
#: 只认数字、不认"五档"这类中文数词：宁可漏判，也不要把"无法回答"这类
#: 纯文字说明误挡掉——漏判的那部分由提示词第 1 条兜。
_NUM_RE = re.compile(r"[0-9\uff10-\uff19]")


def _has_number(text: str) -> bool:
    return bool(_NUM_RE.search(text or ""))


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


def _known_constants(cfg: Config) -> list[float]:
    """护栏与预算的配置值。模型引用它们是对的，不该被当成"追溯不到的数"。"""
    out: list[float] = []
    for section in ("guard", "agent", "planner"):
        for v in (cfg.raw.get(section) or {}).values():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out


def _grounding_mode(cfg: Config) -> str:
    """接地校验的档位：off / shadow / enforce。

    默认 shadow —— 只记不拦。这条判定的误判代价直接落在正确答案上，
    而它的误判率只能在真实流量上量，不能在本机凭样例拍。先影子跑一轮，
    拿到数再决定切不切 enforce。
    """
    mode = str((cfg.raw.get("agent", {}) or {}).get("grounding", "shadow")).lower()
    return mode if mode in ("off", "shadow", "enforce") else "shadow"


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
            converged: str = "", ungrounded: list[str] | None = None) -> AskResult:
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
        ungrounded_numbers=list(ungrounded or []),
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
    # 输入是拿去做嵌入的问句本身，输出是**喂进提示词的表结构全文** ——
    # 后者才是判「模型为什么没用那张表」的第一手材料：召回对了但结构没渲染
    # 出某一列，与压根没召回那张表，在 note 的"召回 N 张表"上完全一样。
    # 嵌入的账记在这一步 —— 它就是这一步花的钱，口径与管道链路（graph 里
    # 那份 embed_kw）逐字一致。mode: all / 关键词回落时三项都是空，不落字段：
    # 记一个 tok_in=0 会让「输入摘要」那列显示 "embed 0 tok"，比占位符更误导。
    # model 记嵌入模型名，不与生成模型混为一谈。
    embed_kw = {"tok_in": rec.data.get("embed_tokens") or 0,
                "cost_cny": rec.data.get("embed_cost_cny") or 0.0,
                "model": rec.data.get("embed_model") or ""} \
        if (rec.data.get("embed_tokens") or rec.data.get("embed_model")) else {}
    tracer.add("schema_recall", t, _brief(rec), tables=tables_hit,
               input=question, output=schema_prompt, **embed_kw)

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
    #: 本轮**每一次**成功执行的结果。接地校验要看全部，不能只看最后一次 ——
    #: 模型的结论经常引用更早几步的数（"全表 447,000 条，其中可售 398,082"）。
    exec_results: list[dict[str, Any]] = []
    scan_blocked: tools.ToolResult | None = None   # 最后一次被 R-11 拦下的执行
    last_error = ""                                # 最后一次 execute_sql 的失败原因
    answer = ""
    converged = ""
    step_count = 0
    gmode = _grounding_mode(cfg)
    ungrounded: list[str] = []
    grounding_retried = False
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
            bad = (grounding.ungrounded(answer, exec_results,
                                        known=_known_constants(cfg))
                   if gmode != "off" and answer else [])
            if bad:
                ungrounded = [grounding.fmt([x]) for x in bad]
                tracer.add("grounding", tracer.start(),
                           f"结论里 {len(bad)} 个数追溯不到查询结果：{grounding.fmt(bad)}",
                           status="blocked" if gmode == "enforce" else "ok")
                # enforce 档先给一次改正机会：把追不到的数点名回灌，让模型去查。
                # 直接拒会把误判的代价全压在正确答案上，而多跑一轮只花一次调用。
                if gmode == "enforce" and not grounding_retried and step < max_steps:
                    grounding_retried = True
                    history.append({
                        "tool": "(接地校验)", "args": {},
                        "brief": f"**你的结论里这些数字没有出现在任何一次查询结果里："
                                 f"{grounding.fmt(bad)}**。它们既不等于某个返回值，也不是"
                                 f"两个返回值做一次加减乘除得到的。请先用 execute_sql 把它们"
                                 f"真正查出来，再重写结论；确实查不到就如实说查不到，不要保留"
                                 f"这些数字。"})
                    answer = ""
                    continue
            else:
                ungrounded = []
            break

        step_count += 1
        res = tools.invoke(action.tool, action.args, ctx)
        tt = tracer.start()
        # 静态 step 名（工具名进 note）：复放/追踪页的步骤映射是静态表，
        # 动态 step id 会显示成原始串（见 tests/test_frontend）。
        # 工具名进结构化 tool 字段（前端 Span 列直接显示），note 只留结果摘要，不再前缀工具名。
        tracer.add("tool_call", tt, _brief(res),
                   status="ok" if res.ok else "blocked", tool=action.tool,
                   # 工具的输入就是模型填的那组参数（execute_sql 的 sql、
                   # get_table_schema 的 table），输出是工具返回的完整数据。
                   # _brief 只给一句"返回 10 行"，看不出返回的是哪 10 行，
                   # 也看不出模型到底把什么 SQL 递了进来。
                   input=_io_json(action.args),
                   output=_io_json(res.data) if res.ok
                          else f"{res.rejected_by or ''} {res.error}".strip())

        # 数据源根本连不上：重试没有价值，继续循环只会把 R-17 预算烧光，
        # 而烧光之后返回的是一句"未完全收敛"，用户看不出真正原因是库挂了。
        # 2026-09-11 跑测里宠物医疗源 10 条里 3 条这么烧掉、1 条据此编了答案。
        if res.data.get("fatal"):
            return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                           rejected_by="DATASOURCE", error=res.error,
                           hint=res.data.get("hint", "") or "请在「数据源」页检查该源的连通性。",
                           tables_hit=tables_hit, step_count=max(1, step_count))

        item: dict[str, Any] = {"tool": action.tool, "args": action.args, "brief": _brief(res)}

        # 高成本查询 → 先给模型一次换写法的机会，还是不行才挂人工审批（HITL）。
        # 原来这里直接 return：模型连"可以改用预聚合汇总表"都来不及试，而 R-11
        # 在生产数据规模下会挡掉大量最基本的问题（2026-09-11 跑测 26/130），
        # 审批页当前又没有入口，用户看到的是一条死路。改为把拒绝连同可行的替代
        # 写法回灌进历史，循环继续；到收尾仍无成功执行时才按 R-11 挂审批 ——
        # 交由 server 既有 _open_approval 建单，带上被拦的 SQL 与预估扫描量。
        if res.rejected_by == "R-11":
            scan_blocked = res
            item["brief"] = (
                f"{res.error}。**这一版不能执行，换个更省的写法再试一次**："
                "① 优先改查同源的预聚合汇总表（表名多为 *_stats_daily / *_daily_stats），"
                "直接对汇总列求和；② 或加时间窗 / 主键区间过滤，分段统计后自行相加；"
                "③ 严禁用抽样（TABLESAMPLE、LIMIT 取样）冒充全量。"
                "若两条路都走不通，finish=true 并如实说明这个口径当前取不到。")
            history.append(item)
            continue

        if action.tool == "execute_sql" and not res.ok:
            last_error = res.error or (res.rejected_by or "")

        if res.ok and action.tool == "execute_sql":
            last_exec = res.data
            exec_results.append({"columns": list(res.data.get("columns") or []),
                                 "rows": list(res.data.get("rows") or [])})
            ctx.last_result = res.data          # 供 analyze_result / export_result 用
            item["preview"] = {"columns": res.data.get("columns", []),
                               "rows": planner.preview_rows(res.data.get("rows", [])),
                               "row_count": res.data.get("row_count")}
        elif res.ok and action.tool == "get_table_schema":
            item["columns"] = [c["name"] for c in res.data.get("columns", [])]
        elif res.ok and action.tool == "search_schema":
            item["columns"] = res.data.get("tables", [])
        history.append(item)
    else:
        converged = f"达步数上限 {max_steps}，收敛作答"

    # 4) 收尾
    #
    # 这一段是"能不能把这个答案给用户"的最后一道判定，纯代码、不问模型。
    # 原来只有一句 `ok = bool(last_exec) or bool(answer)` —— 只要模型吐了字就算成功，
    # 于是"一次 SQL 都没发、直接写个整数再补一段口径说明"照样返回 ok=true。
    # 2026-09-11 跑测里 130 条有 15 条是这么来的，数量级差 2~3 个（订单总数答 1 万、
    # 实际 120 万），界面上无从分辨。下面三条都是确定性判定：
    common = dict(tables_hit=tables_hit, step_count=max(1, step_count), converged=converged,
                  ungrounded=ungrounded)

    if last_exec is not None and ungrounded and gmode == "enforce":
        # 给过一次改正机会了还是追溯不到 —— 这些数不是从库里来的，不能递出去。
        # 与 NO_EVIDENCE 分开是因为两者的处置不同：那条是"一次都没跑"，
        # 这条是"跑了，但答案没用上跑出来的东西"。
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="UNGROUNDED", last_exec=last_exec,
                       error=f"结论里这些数字追溯不到任何一次查询结果："
                             f"{'、'.join(ungrounded)}，因此不给出这个答案。",
                       hint="换个更具体的问法，或在「直查 SQL」里自己跑一条核对；"
                            "结果表仍在下方，可直接看。",
                       **common)

    if last_exec is not None:
        # 有数据。模型没来得及归因时，别用一句"未完全收敛"把已经查到的结果盖掉 ——
        # 结果表就在 AskResult 里，直说"看表"比丢掉它诚实得多。
        if not answer:
            answer = (("（未在预算内完成归因）" + converged + "。") if converged else "") + \
                     "以下为最后一次查询执行的原始结果，请直接看结果表。"
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=True,
                       reasoning=answer, last_exec=last_exec, **common)

    # 以下都是"本轮没有一次成功的 execute_sql"。
    if scan_blocked is not None:
        # 换过写法仍然过不去：按 R-11 挂审批，交由 server 建单。
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="R-11", last_exec=scan_blocked.data,
                       error=scan_blocked.error, **common)

    tail = f"（最后一次查询执行失败：{last_error}）" if last_error else ""
    if _has_number(answer):
        # **本条是 P0 兜底**：没取到数据就不许出数字。这里刻意不把模型那段话
        # 回显给用户 —— 它正是编造出来的内容，回显等于换个位置继续骗人。
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="NO_EVIDENCE",
                       error="本轮没有任何一次查询执行成功，因此不给出带数字的结论。" + tail,
                       hint="换个更具体的问法，或先确认这个口径需要的表是否可查；"
                            "也可在「直查 SQL」里自己跑一条核对。",
                       **common)
    if not answer:
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="NO_RESULT", error="未能产出结果" + tail, **common)
    # 不含任何数字的定性回答（例如"这个库里有哪些表"）没有可编造的量，放行。
    return _result(cfg, question, trace_id, thread_id, org, tracer, ok=True,
                   reasoning=answer, **common)
