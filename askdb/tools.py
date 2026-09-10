"""可信数据 Agent 的工具层（v2 设计 §1 / §4 / §7）。

把三块**已有能力**抽成独立、可被 LLM 选择调用的原子工具，附**分层声明**与注册表：

  · search_schema      向量召回相关表/字段/口径          —— 只读
  · get_table_schema   取某表精确字段/枚举               —— 只读
  · execute_sql        护栏 → 干跑 → 只读执行 → 脱敏      —— 只读（安全原子）

这一层是"把固定管道拆成工具"，不改动现有 graph 链路：三个函数分别包装
schema_rag.recall / Config.tables / (guard.check + Executor)，行为与管道里
逐节点一致。LLM 只负责"选哪个工具、传什么参数"；每个工具的安全闸由工具自身
强制，LLM 无法绕过（设计见 docs/design-trusted-data-agent-v2.html）。

**上下文一律走 Config。** org / tenant / role / source 都挂在 cfg 上，调用方须先
构造好收窄后的 cfg（server._cfg_for → _scoped，等价 sources.derive_config →
identity.for_roles），工具不再层层传这些参数 —— 与现有链路同一条约定。

**分层是安全边界，不是分类装饰。** 只读 / 分析工具可直接执行；副作用工具
（发邮件 / 导出 / 写回）越出只读边界，必须经人工确认闸，绝不由 LLM 直接触发。
注入自有工具时**必须声明层级**，Runtime 按层级挂闸 —— LLM 没有"跳过闸"的选项。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import guard, schema_rag
from .config import Config
from .executor import Executor, MaskUnresolved


class Tier(str, Enum):
    """工具层级 —— 决定 Runtime 给它挂哪种闸。"""

    READ = "read"                # 只读、无副作用、在安全边界内：LLM 可自由链式调用
    ANALYZE = "analyze"          # 对已过闸结果计算、无对外副作用：沙箱、只吃已脱敏结果
    SIDE_EFFECT = "side_effect"  # 越出只读边界（发邮件/导出/写回）：必须人工确认闸


@dataclass
class ToolContext:
    """一次工具调用的运行时上下文 —— 不由 LLM 提供，由 Runtime 注入。"""

    cfg: Config
    org_id: int
    #: 复用同一个只读执行器可省去重复建连；不传则按 cfg 现建一个。
    executor: Executor | None = None
    #: 上一步 execute_sql 的**已脱敏**结果。分析/导出类工具只吃它，不再回库
    #: —— 这是"分析工具不得成为绕过安全闸的新通道"（§10.1）的落点。
    last_result: dict[str, Any] | None = None


@dataclass
class ToolResult:
    """工具调用的结构化产出。data 里放各工具自己的字段。"""

    ok: bool
    tool: str
    data: dict[str, Any] = field(default_factory=dict)
    rejected_by: str | None = None   # 被护栏/闸拦下时的规则号（R-xx / P03 …）
    error: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "tool": self.tool, "data": self.data,
            "rejected_by": self.rejected_by, "error": self.error, "note": self.note,
        }


# --------------------------------------------------------------------------
# 三个只读原子
# --------------------------------------------------------------------------
def search_schema(question: str, cfg: Config) -> ToolResult:
    """向量召回与问题相关的表/字段/业务口径。

    包装 schema_rag.recall：top_k / token_budget / mode 等仍从 cfg.raw['schema_rag']
    读，与管道一致。返回 .prompt（可直接注入的 schema 文本）与命中表名，并把
    盲选 / 降级两个可信度信号如实带出。
    """
    r = schema_rag.recall(question, cfg)
    return ToolResult(
        ok=True, tool="search_schema",
        data={
            "tables": r.table_names,
            "metrics": [m.name for m in r.metrics],
            "prompt": r.prompt,
            "blind": bool(r.blind),
            "degraded": bool(getattr(r, "degraded_from", None)),
            "truncated": list(r.truncated),
            "mode": r.mode,
            "note": r.note or "",
        },
        note=r.note or "",
    )


def get_table_schema(table: str, cfg: Config) -> ToolResult:
    """取某表的精确字段、类型、枚举、租户/敏感标记。

    只读 cfg.tables（已按角色收窄）——表不在可见白名单即视作不存在，如实回列
    可见表名，不泄露收窄掉的表。
    """
    t = cfg.tables.get(table)
    if t is None:                       # 大小写不敏感兜底
        low = table.strip().lower()
        t = next((v for k, v in cfg.tables.items() if k.lower() == low), None)
    if t is None:
        return ToolResult(
            ok=False, tool="get_table_schema",
            error=f"表不存在或不在可见白名单：{table}",
            data={"available": list(cfg.tables.keys())},
        )
    cols = [
        {"name": c.name, "type": c.type, "desc": c.desc, "enum": list(c.enum),
         "tenant": c.tenant, "sensitive": c.sensitive}
        for c in t.columns.values()
    ]
    return ToolResult(
        ok=True, tool="get_table_schema",
        data={"table": t.name, "desc": t.desc, "aliases": list(t.aliases),
              "tenant_column": t.tenant_column, "columns": cols},
    )


def execute_sql(sql: str, cfg: Config, org_id: int,
                executor: Executor | None = None) -> ToolResult:
    """安全原子：护栏(AST) → 干跑(EXPLAIN) → 只读执行 → 脱敏。

    与管道 _n_guard / _n_dry_run / _n_execute 三节点同一套判定，**每次调用独立过闸**。
    rejected_by 的来源：guard(R-xx) / dry_run(R-11 超扫描阈值，需人工审批) /
    execute(P03 脱敏无法解析)。脱敏与 masked_columns 由 Executor.run 内部产出。
    """
    g = guard.check(sql, cfg, org_id, dialect=cfg.dialect, question="")
    if not g.ok:
        return ToolResult(
            ok=False, tool="execute_sql", rejected_by=g.rejected_by, error=g.reason,
            data={"rules_fired": list(g.rules_fired), "out_of_scope": g.out_of_scope},
        )
    final = g.sql
    ex = executor or Executor(cfg)

    # 干跑：估扫描行数。超阈值不是终结，是挂起等人工审批（R-11，交由 Runtime 接线）。
    explain_rows: int | None = None
    try:
        exp = ex.explain(final)
        explain_rows = exp.est_rows
    except Exception:
        exp = None
    max_scan = int(cfg.raw.get("guard", {}).get("max_scan_rows", 200000))
    if explain_rows is not None and explain_rows > max_scan:
        return ToolResult(
            ok=False, tool="execute_sql", rejected_by="R-11",
            error=f"预估扫描 {explain_rows} 行，超过上限 {max_scan}，需人工审批放行",
            data={"sql_final": final, "explain_rows": explain_rows, "needs_approval": True},
        )

    ex.set_org(org_id)
    try:
        q = ex.run(final, limit_capped=("R-09" in g.rules_fired))
    except MaskUnresolved:
        return ToolResult(
            ok=False, tool="execute_sql", rejected_by="P03",
            error="脱敏判定无法解析该 SQL，从严拒绝返回",
            data={"sql_final": final},
        )
    except Exception as e:
        return ToolResult(
            ok=False, tool="execute_sql", error=str(e),
            data={"sql_final": final},
        )
    return ToolResult(
        ok=True, tool="execute_sql",
        data={
            "sql_final": final, "columns": list(q.columns), "rows": q.rows,
            "row_count": q.row_count, "truncated": q.truncated, "as_of": q.as_of,
            "elapsed_ms": q.elapsed_ms, "masked_columns": list(q.masked_columns),
            "mask_degraded": q.mask_degraded, "rules_fired": list(g.rules_fired),
            "rewrites": list(g.rewrites), "explain_rows": explain_rows,
        },
    )


# --------------------------------------------------------------------------
# 第 2 层：分析（对已过闸结果计算，无对外副作用）
# --------------------------------------------------------------------------
def analyze_result(ctx: ToolContext) -> ToolResult:
    """对上一步 execute_sql 的**已脱敏**结果做汇总统计 —— 只吃 ctx.last_result，
    不再回库。脱敏列跳过数值统计（打码值本就不该参与计算）。"""
    d = ctx.last_result
    if not d or not d.get("rows"):
        return ToolResult(ok=False, tool="analyze_result",
                          error="没有可分析的结果，请先用 execute_sql 取数")
    cols = d.get("columns", [])
    rows = d.get("rows", [])
    masked = set(d.get("masked_columns", []))
    stats = []
    for i, c in enumerate(cols):
        vals = [r[i] for r in rows if i < len(r)]
        nonnull = [v for v in vals if v is not None]
        entry: dict[str, Any] = {"column": c, "count": len(nonnull),
                                 "distinct": len({str(v) for v in nonnull})}
        if c in masked:
            entry["note"] = "已脱敏，跳过数值统计"
        else:
            nums = []
            ok_num = True
            for v in nonnull:
                try:
                    nums.append(float(str(v).replace(",", "")))
                except (ValueError, TypeError):
                    ok_num = False
                    break
            if ok_num and nums:
                entry.update(min=min(nums), max=max(nums),
                             mean=round(sum(nums) / len(nums), 4), sum=round(sum(nums), 4))
        stats.append(entry)
    return ToolResult(ok=True, tool="analyze_result",
                      data={"n_rows": len(rows), "stats": stats})


# --------------------------------------------------------------------------
# 第 3 层：副作用（越出只读边界）—— 只能经人工确认由 Runtime 放行，LLM 不能直接触发
# --------------------------------------------------------------------------
def export_result(ctx: ToolContext, fmt: str = "csv") -> ToolResult:
    """把上一步**已脱敏**结果导出为 CSV/JSON 文本。副作用工具：真正的落盘/投递
    由部署方接，这里产出载荷。**不由 LLM 直接触发**（见 invoke / run_side_effect）。"""
    import csv
    import io
    import json as _json

    d = ctx.last_result
    if not d or not d.get("rows"):
        return ToolResult(ok=False, tool="export_result", error="没有可导出的结果")
    cols = d.get("columns", [])
    rows = d.get("rows", [])
    if fmt == "json":
        payload = _json.dumps([dict(zip(cols, r)) for r in rows], ensure_ascii=False)
    else:
        fmt = "csv"
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow(r)
        payload = buf.getvalue()
    return ToolResult(ok=True, tool="export_result",
                      data={"format": fmt, "rows": len(rows), "payload": payload})


# --------------------------------------------------------------------------
# 工具规格 + 注册表
# --------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    tier: Tier
    summary: str                       # 一句话，喂给 LLM 做选择
    params: dict[str, str]             # LLM 可传的参数 -> 说明（cfg/org 等上下文不在此列）
    _fn: Callable[[dict[str, Any], ToolContext], ToolResult]

    def spec(self) -> dict[str, Any]:
        """喂给 LLM 的工具描述。"""
        return {"name": self.name, "tier": self.tier.value,
                "summary": self.summary, "params": self.params}

    def call(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return self._fn(args, ctx)


def _t_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    q = (args or {}).get("question")
    if not q:
        return ToolResult(ok=False, tool="search_schema", error="缺少参数 question")
    return search_schema(str(q), ctx.cfg)


def _t_get(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tbl = (args or {}).get("table")
    if not tbl:
        return ToolResult(ok=False, tool="get_table_schema", error="缺少参数 table")
    return get_table_schema(str(tbl), ctx.cfg)


def _t_exec(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    sql = (args or {}).get("sql")
    if not sql:
        return ToolResult(ok=False, tool="execute_sql", error="缺少参数 sql")
    return execute_sql(str(sql), ctx.cfg, ctx.org_id, ctx.executor)


def _t_analyze(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return analyze_result(ctx)


def _t_export(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return export_result(ctx, str((args or {}).get("format", "csv")))


REGISTRY: dict[str, Tool] = {
    "search_schema": Tool(
        "search_schema", Tier.READ,
        "向量召回与问题相关的表/字段/业务口径，返回可注入的 schema 提示词与命中表名",
        {"question": "自然语言查询意图（一句话）"}, _t_search),
    "get_table_schema": Tool(
        "get_table_schema", Tier.READ,
        "取某一张表的精确字段、类型、枚举取值——拿不准列名/口径时先查它，别猜",
        {"table": "表名"}, _t_get),
    "execute_sql": Tool(
        "execute_sql", Tier.READ,
        "执行一条只读 SQL：自动过护栏(AST)、干跑估行、只读执行、脱敏；返回结果行",
        {"sql": "一条只读 SELECT/CTE"}, _t_exec),
    "analyze_result": Tool(
        "analyze_result", Tier.ANALYZE,
        "对上一步 execute_sql 的结果做汇总统计（计数/去重/数值 min-max-mean-sum），不再回库",
        {}, _t_analyze),
    "export_result": Tool(
        "export_result", Tier.SIDE_EFFECT,
        "把上一步结果导出为 CSV/JSON（副作用：需人工确认后由 Runtime 放行，不能直接触发）",
        {"format": "csv 或 json"}, _t_export),
}


def tool_specs(tiers: tuple[Tier, ...] = (Tier.READ, Tier.ANALYZE)) -> list[dict[str, Any]]:
    """列出可暴露给 LLM 选择的工具规格。默认只给只读原子。

    工具太多会拉低选择准确率、挤爆上下文预算，所以按层级/任务上下文控制暴露面
    —— 副作用工具默认不出现，需要时由 Runtime 显式挂出并强制人工确认。
    """
    return [t.spec() for t in REGISTRY.values() if t.tier in tiers]


def invoke(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """按名调用工具，并在 Tool 边界强制层级闸。

    只读 / 分析工具直接执行；**副作用工具一律不由此路直接触发** —— 它必须先经
    人工确认闸（后续步骤接线到 approval/HITL）。这就是"安全钉在 Tool 边界"的落点。
    """
    t = REGISTRY.get(name)
    if t is None:
        return ToolResult(ok=False, tool=name, error=f"未知工具：{name}",
                          data={"available": list(REGISTRY.keys())})
    if t.tier is Tier.SIDE_EFFECT:
        return ToolResult(
            ok=False, tool=name,
            error="副作用工具必须经人工确认闸，不能由 LLM 直接触发",
            note="HITL required")
    return t.call(args, ctx)


def run_side_effect(name: str, args: dict[str, Any], ctx: ToolContext,
                    approved: bool = False) -> ToolResult:
    """副作用工具（发邮件/导出/写回）的**唯一**执行路径 —— 由 Runtime 在**人工确认后**
    调用，approved 必须为真。这就是"副作用工具在哪里被调用"的答案：不在 LLM 决策里，
    而在这条经人工放行的 Runtime 通道里（设计 §工具体系与注入）。"""
    t = REGISTRY.get(name)
    if t is None or t.tier is not Tier.SIDE_EFFECT:
        return ToolResult(ok=False, tool=name, error=f"不是副作用工具或不存在：{name}")
    if not approved:
        return ToolResult(ok=False, tool=name, note="HITL required",
                          error="副作用动作需人工确认后由 Runtime 放行")
    return t.call(args, ctx)
