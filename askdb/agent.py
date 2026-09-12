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

8. **一个实体一行，不要把多行拼成一格。** 问"每个 X 的 Y"就 GROUP BY X 正常返回
   N 行，禁止用 string_agg / group_concat / listagg / 手工 CONCAT 把 N 个实体
   压成一行一列的长串（`WECHAT: total=460323 | ALIPAY: total=415330 | …`）。
   三个代价都是实打实的：
   · **行数护栏按行计**。拼成一格后九个渠道算 1 行，R-13 的行数上限形同虚设 ——
     一格里能塞进任意多实体。
   · **脱敏按投影里的列做**。一格里只要混进一个敏感列，整格会被一起打成星号，
     连同其它实体的所有数字一起毁掉（executor._mask 对一个投影取 any）。
   · 结果表退化成 1 行 1 列，没法按列排序、比较、导出，读的人只能盯着一条长串
     自己数。
   要一行一个汇总串的场合（例如"把这些标签列出来"）才用聚合拼接，且只在
   GROUP BY 的组内拼，不要跨整个结果集拼。

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
              trace_id: str | None = None, thread_id: str | None = None,
              clarification: str = "") -> AskResult:
    """自主决策入口。返回与 graph.ask 同一套 AskResult，并写审计（收尾）。

    clarification 是发起人事后补上的条件（「补充条件」「换个问法」走这里）。
    它进决策历史而**不改写 question** —— question 是这条线程的身份，
    审计标题与审批指纹都读它，就地改掉会让同一条线程变成另一个问题。

    审计是任务中心 / 复核队列 / replay 的共同数据源：它们都从审计记录派生
    （audit.tasks / audit.needs_review），所以 agent 链路一旦如实写审计，这三样
    立刻复用现网机制，无需各造一套。
    """
    result = _drive(question, cfg, org_id, executor=executor, llm=llm,
                    trace_id=trace_id, thread_id=thread_id,
                    clarification=clarification)
    try:                                  # 审计不该成为查询失败的原因
        write_audit(cfg, _audit_of(result, cfg, "ask"))
    except Exception:
        pass
    return result


def _drive(question: str, cfg: Config, org_id: int | None = None, *,
           executor: Executor | None = None, llm: LlmClient | None = None,
           trace_id: str | None = None, thread_id: str | None = None,
           clarification: str = "") -> AskResult:
    """跑一次 agent 图。

    2026-09-12 从 `for step in range(...)` 改成 LangGraph（见 agentgraph）。
    换掉的是控制流，**每一条判定都原样搬了过去**；换来的是检查点覆盖到 agent
    链路，于是「创建任务 / 补充条件 / 换个问法」不必再为了续跑退回老管道 ——
    那条分流的代价是：最该深挖的请求反而被送进只能猜列名的链路。
    """
    from . import agentgraph

    trace_id = trace_id or uuid.uuid4().hex[:12]
    thread_id = thread_id or trace_id
    org = org_id if org_id is not None else int(
        cfg.raw.get("tenant", {}).get("default_ctx", 0) or 0)
    tracer = Tracer()

    # 每日配额快速失败（与管道同一口径）。**进图之前判**，一个 token 都不花。
    dq = build_quota(cfg)
    over, used = dq.exhausted()
    if over:
        tracer.add("quota", tracer.start(), f"当日已用 {used}/{dq.limit}", status="blocked")
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="QUOTA", error=f"已达当日模型调用上限（{used}/{dq.limit}）",
                       hint="明日自动恢复；直查 SQL 不受配额限制。")

    # 发起记录先落盘：进程中途被杀时检查点/审计里仍有这条线程，任务中心据此
    # 列得出、凭 thread_id 续得上（与管道 _execute 同一处理）。
    try:
        write_audit(cfg, {
            "trace_id": trace_id, "ts": now_iso(), "kind": "ask",
            "phase": PHASE_STARTED, "thread_id": thread_id, "org_id": org,
            "question": question, "role": cfg.role or "ANONYMOUS",
            "user": cfg.user or "",
            "source": cfg.source_id or "builtin",
            "source_name": cfg.source_name or cfg.path,
        })
    except Exception:
        pass

    own_exec = executor is None
    ex = executor or Executor(cfg)
    client = llm or LlmClient(cfg)
    max_steps, cost_cap = _budget(cfg)
    deps = agentgraph.Deps(
        cfg=cfg, llm=client, executor=ex, tracer=tracer,
        ctx=tools.ToolContext(cfg=cfg, org_id=org, executor=ex))
    init = agentgraph.initial_state(question, org, trace_id, thread_id,
                                    max_steps, cost_cap, clarification)
    try:
        final = agentgraph.ensure_graph(cfg).invoke(
            init,
            {"configurable": {"thread_id": thread_id, "deps": deps},
             "recursion_limit": agentgraph.recursion_limit(max_steps)})
    except Exception as e:                        # noqa: BLE001
        # 图本身抛出来（递归上限、检查点库故障）。**不能让它变成裸 500** ——
        # 用户看到的必须是一句能理解的话，且这条线程要如实收尾，否则它会
        # 永远停在「运行中」（那正是 2026-09-11 那 8 条僵尸线程的来路）。
        tracer.add("finalize", tracer.start(), f"执行图异常：{e}", status="failed")
        return _result(cfg, question, trace_id, thread_id, org, tracer, ok=False,
                       rejected_by="EXEC", error=f"执行链路异常：{e}",
                       hint="这不是提问本身的问题，稍后重试；持续出现请联系运维。")
    finally:
        if own_exec:
            ex.close()
    return agentgraph.to_result(final, cfg, tracer)
