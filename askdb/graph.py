"""LangGraph 状态机 —— 链路主干。

选择状态机而非线性链的原因：存在条件分支（校验失败回边）与重试计数，
且需支持检查点以便失败复现（技术设计说明书 §5）。

P0 为单步链路；plan / assess 两个节点与重规划回边在 P5 补齐，
届时只需新增节点与条件边，现有节点不动。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph

from . import guard, schema_rag
from .config import Config
from .executor import DataSourceError, Executor
from .llm import LlmClient, LlmNotConfigured
from .trace import Tracer, cost_cny, write_audit


class AskState(TypedDict, total=False):
    question: str
    org_id: int
    trace_id: str

    recall: schema_rag.Recall
    sql_raw: str
    sql_final: str
    reasoning: str
    guard_result: guard.GuardResult

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool

    error: str | None
    error_hint: str
    rejected_by: str | None
    attempt: int

    _cfg: Config
    _llm: LlmClient
    _exec: Executor
    _tracer: Tracer


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

    rejected_by: str | None = None
    error: str = ""
    hint: str = ""

    tables_hit: list[str] = field(default_factory=list)
    metrics_hit: list[str] = field(default_factory=list)
    attempts: int = 1
    steps: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cost_cny: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["rows"] = [[_jsonable(v) for v in r] for r in self.rows]
        return d


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


# --------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------

def _n_retrieve(state: AskState) -> dict[str, Any]:
    cfg, tr = state["_cfg"], state["_tracer"]
    t = tr.start()
    r = schema_rag.recall(state["question"], cfg)
    note = f"命中 {len(r.tables)} 张表（白名单共 {len(cfg.tables)} 张）"
    if r.metrics:
        note += f"；命中口径 {'、'.join(m.name for m in r.metrics)}"
    if r.truncated:
        note += f"；因 token 预算裁掉 {'、'.join(r.truncated)}"
    tr.add("schema_recall", t, note)
    return {"recall": r}


def _n_generate(state: AskState) -> dict[str, Any]:
    tr, llm = state["_tracer"], state["_llm"]
    attempt = state.get("attempt", 0)
    t = tr.start()
    try:
        draft, usage = llm.generate_sql(
            question=state["question"],
            schema_prompt=state["recall"].prompt,
            last_sql=state.get("sql_raw", ""),
            error=state.get("error") or "",
        )
    except LlmNotConfigured as e:
        tr.add("generate_sql", t, "未配置模型密钥", status="failed")
        return {"error": str(e), "error_hint": "配置密钥后重试", "rejected_by": "LLM"}
    except Exception as e:
        tr.add("generate_sql", t, f"模型调用失败：{e}", status="failed")
        return {
            "error": f"模型调用失败：{e}",
            "error_hint": "检查网络与密钥是否有效；也可稍后重试。",
            "rejected_by": "LLM",
        }

    label = "生成 1 条 SELECT" if attempt == 0 else f"第 {attempt + 1} 轮重新生成"
    tr.add("generate_sql", t, label, tok_in=usage.input_tokens, tok_out=usage.output_tokens)
    if not (draft.sql or "").strip():
        return {
            "error": draft.reasoning or "模型判断当前表结构无法回答该问题。",
            "error_hint": "换个问法，或在 config/tables.yaml 中开放更多表。",
            "rejected_by": "NO_SQL",
            "reasoning": draft.reasoning,
        }
    return {"sql_raw": draft.sql, "reasoning": draft.reasoning, "error": None, "rejected_by": None}


def _n_guard(state: AskState) -> dict[str, Any]:
    cfg, tr = state["_cfg"], state["_tracer"]
    t = tr.start()
    r = guard.check(state["sql_raw"], cfg, org_id=state["org_id"])
    if not r.ok:
        tr.add("guard", t, f"{r.rejected_by} {r.reason}", status="blocked")
        return {"guard_result": r, "error": r.reason, "rejected_by": r.rejected_by}
    note = "；".join(r.rewrites) or "无需改写"
    tr.add("guard", t, note)
    return {"guard_result": r, "sql_final": r.sql, "error": None, "rejected_by": None}


def _n_dry_run(state: AskState) -> dict[str, Any]:
    tr, ex = state["_tracer"], state["_exec"]
    t = tr.start()
    r = ex.explain(state["sql_final"])
    if not r.ok:
        tr.add("dry_run", t, r.reason, status="blocked")
        return {
            "error": r.reason,
            "error_hint": "缩小时间范围或加筛选条件，让扫描量降下来。",
            "rejected_by": "R-11",
        }
    est = f"预估扫描 {r.est_rows:,} 行" if r.est_rows is not None else "计划无基数估计"
    tr.add("dry_run", t, est)
    return {"error": None, "rejected_by": None}


def _n_execute(state: AskState) -> dict[str, Any]:
    tr, ex = state["_tracer"], state["_exec"]
    t = tr.start()
    try:
        res = ex.run(state["sql_final"])
    except DataSourceError as e:
        tr.add("execute", t, str(e), status="failed")
        return {"error": str(e), "error_hint": e.hint, "rejected_by": "EXEC"}
    except Exception as e:
        tr.add("execute", t, f"执行失败：{e}", status="failed")
        return {"error": f"执行失败：{e}", "rejected_by": None}

    note = f"返回 {res.row_count} 行"
    if res.truncated:
        note += "（已按行数上限截断）"
    tr.add("execute", t, note)
    return {
        "columns": res.columns, "rows": res.rows, "row_count": res.row_count,
        "truncated": res.truncated, "error": None, "rejected_by": None,
    }


def _n_reflect(state: AskState) -> dict[str, Any]:
    tr = state["_tracer"]
    t = tr.start()
    n = state.get("attempt", 0) + 1
    tr.add("reflect", t, f"第 {n} 次重试：把真实错误回灌模型重新生成")
    return {"attempt": n}


def _n_finalize(state: AskState) -> dict[str, Any]:
    tr = state["_tracer"]
    t = tr.start()
    tr.add("finalize", t, "已附最终 SQL 与判定链路")
    return {}


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

def _route_after_generate(state: AskState) -> Literal["guard", "finalize"]:
    return "finalize" if state.get("rejected_by") in ("LLM", "NO_SQL") else "guard"


def _route_after_guard(state: AskState) -> Literal["dry_run", "reflect", "finalize"]:
    if not state.get("rejected_by"):
        return "dry_run"
    cfg: Config = state["_cfg"]
    return "reflect" if state.get("attempt", 0) < cfg.max_retry else "finalize"


def _route_after_dry_run(state: AskState) -> Literal["execute", "finalize"]:
    return "finalize" if state.get("rejected_by") else "execute"


def _route_after_execute(state: AskState) -> Literal["finalize", "reflect"]:
    if not state.get("error"):
        return "finalize"
    cfg: Config = state["_cfg"]
    # 数据源不可用不是模型的错，重试没有意义
    if state.get("rejected_by") == "EXEC":
        return "finalize"
    return "reflect" if state.get("attempt", 0) < cfg.max_retry else "finalize"


def build_graph():
    g = StateGraph(AskState)
    g.add_node("retrieve", _n_retrieve)
    g.add_node("generate", _n_generate)
    g.add_node("guard", _n_guard)
    g.add_node("dry_run", _n_dry_run)
    g.add_node("execute", _n_execute)
    g.add_node("reflect", _n_reflect)
    g.add_node("finalize", _n_finalize)

    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "generate")
    g.add_conditional_edges("generate", _route_after_generate,
                            {"guard": "guard", "finalize": "finalize"})
    g.add_conditional_edges("guard", _route_after_guard,
                            {"dry_run": "dry_run", "reflect": "reflect", "finalize": "finalize"})
    g.add_conditional_edges("dry_run", _route_after_dry_run,
                            {"execute": "execute", "finalize": "finalize"})
    g.add_conditional_edges("execute", _route_after_execute,
                            {"finalize": "finalize", "reflect": "reflect"})
    g.add_edge("reflect", "generate")
    g.add_edge("finalize", END)
    return g.compile()


_GRAPH = None


def ask(
    question: str,
    cfg: Config,
    org_id: int | None = None,
    executor: Executor | None = None,
    llm: LlmClient | None = None,
) -> AskResult:
    """跑一次完整链路。executor / llm 可注入，便于测试与复用连接。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()

    own_exec = executor is None
    ex = executor or Executor(cfg)
    tracer = Tracer()
    org = cfg.default_org if org_id is None else org_id
    trace_id = uuid.uuid4().hex[:12]

    init: AskState = {
        "question": question, "org_id": org, "trace_id": trace_id, "attempt": 0,
        "_cfg": cfg, "_llm": llm or LlmClient(cfg), "_exec": ex, "_tracer": tracer,
    }
    try:
        out = _GRAPH.invoke(init, {"recursion_limit": 40})
    finally:
        if own_exec:
            ex.close()

    gr: guard.GuardResult | None = out.get("guard_result")
    recall_obj: schema_rag.Recall | None = out.get("recall")
    tok_in, tok_out = tracer.tok_in, tracer.tok_out

    result = AskResult(
        ok=not out.get("error") and bool(out.get("sql_final")),
        question=question, trace_id=trace_id, org_id=org,
        sql_raw=out.get("sql_raw", ""), sql_final=out.get("sql_final", ""),
        reasoning=out.get("reasoning", ""),
        rules_fired=list(gr.rules_fired) if gr else [],
        rewrites=list(gr.rewrites) if gr else [],
        columns=out.get("columns", []), rows=out.get("rows", []),
        row_count=out.get("row_count", 0), truncated=out.get("truncated", False),
        rejected_by=out.get("rejected_by"), error=out.get("error") or "",
        hint=out.get("error_hint", ""),
        tables_hit=recall_obj.table_names if recall_obj else [],
        metrics_hit=[m.name for m in recall_obj.metrics] if recall_obj else [],
        attempts=out.get("attempt", 0) + 1,
        steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        tok_in=tok_in, tok_out=tok_out,
        cost_cny=cost_cny(tok_in, tok_out, cfg.llm),
    )

    write_audit(cfg.audit_log, {
        "trace_id": trace_id, "org_id": org, "question": question,
        "tables_hit": result.tables_hit, "metrics_hit": result.metrics_hit,
        "sql_raw": result.sql_raw, "sql_final": result.sql_final,
        "rules_fired": result.rules_fired, "rejected_by": result.rejected_by,
        "attempts": result.attempts, "rows_returned": result.row_count,
        "elapsed_ms": result.elapsed_ms, "tok_in": tok_in, "tok_out": tok_out,
        "cost_cny": result.cost_cny, "steps": result.steps,
    })
    return result
