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
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from . import guard, schema_rag
from .config import Config
from .executor import DataSourceError, Executor
from .llm import LlmClient, LlmNotConfigured
from .trace import Tracer, cost_cny, now_iso, today_calls, write_audit


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
    explain_rows: int | None

    error: str | None
    error_hint: str
    rejected_by: str | None
    attempt: int


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

def _n_retrieve(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    t = d.tracer.start()
    r = schema_rag.recall(state["question"], d.cfg)
    note = f"命中 {len(r.tables)} 张表（白名单共 {len(d.cfg.tables)} 张）"
    if r.metrics:
        note += f"；命中口径 {'、'.join(m.name for m in r.metrics)}"
    if r.truncated:
        note += f"；因 token 预算裁掉 {'、'.join(r.truncated)}"
    d.tracer.add("schema_recall", t, note)
    return {
        "schema_prompt": r.prompt,
        "tables_hit": r.table_names,
        "metrics_hit": [m.name for m in r.metrics],
        "recall_truncated": r.truncated,
    }


def _n_generate(state: AskState, config: RunnableConfig) -> dict[str, Any]:
    d = _deps(config)
    attempt = state.get("attempt", 0)
    t = d.tracer.start()
    try:
        draft, usage = d.llm.generate_sql(
            question=state["question"],
            schema_prompt=state["schema_prompt"],
            last_sql=state.get("sql_raw", ""),
            error=state.get("error") or "",
        )
    except LlmNotConfigured as e:
        d.tracer.add("generate_sql", t, "未配置模型密钥", status="failed")
        return {"error": str(e), "error_hint": "配置密钥后重试", "rejected_by": "LLM"}
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
    r = guard.check(state["sql_raw"], d.cfg, org_id=state["org_id"])
    if not r.ok:
        d.tracer.add("guard", t, f"{r.rejected_by} {r.reason}", status="blocked")
        return {"error": r.reason, "rejected_by": r.rejected_by}
    note = "；".join(r.rewrites) or "无需改写"
    d.tracer.add("guard", t, note)
    return {
        "sql_final": r.sql, "rules_fired": r.rules_fired, "rewrites": r.rewrites,
        "error": None, "rejected_by": None,
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
        res = d.executor.run(state["sql_final"])
    except DataSourceError as e:
        d.tracer.add("execute", t, str(e), status="failed")
        return {"error": str(e), "error_hint": e.hint, "rejected_by": "EXEC"}
    except Exception as e:
        d.tracer.add("execute", t, f"执行失败：{e}", status="failed")
        return {"error": f"执行失败：{e}", "rejected_by": None}

    note = f"返回 {res.row_count} 行"
    if res.truncated:
        note += "（已按行数上限截断）"
    d.tracer.add("execute", t, note)
    return {
        "columns": [str(c) for c in res.columns],
        "rows": [[_jsonable(v) for v in row] for row in res.rows],
        "row_count": res.row_count, "truncated": res.truncated,
        "error": None, "rejected_by": None,
    }


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
    return "reflect" if _can_retry(state) else "finalize"


def _route_after_dry_run(state: AskState) -> Literal["execute", "reflect", "finalize"]:
    if not state.get("rejected_by"):
        return "execute"
    if state.get("rejected_by") == "EXEC":
        return "finalize"
    # 干跑失败两种情形都值得重试：计划生成失败是语义错，
    # 扫描量超限则可以让模型补上筛选条件。次数仍受 R-14 约束。
    return "reflect" if _can_retry(state) else "finalize"


def _route_after_execute(state: AskState) -> Literal["finalize", "reflect"]:
    if not state.get("error"):
        return "finalize"
    # 数据源不可用不是模型的错，重试没有意义
    if state.get("rejected_by") == "EXEC":
        return "finalize"
    return "reflect" if _can_retry(state) else "finalize"


def _build_skeleton() -> StateGraph:
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
                            {"execute": "execute", "reflect": "reflect", "finalize": "finalize"})
    g.add_conditional_edges("execute", _route_after_execute,
                            {"finalize": "finalize", "reflect": "reflect"})
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

    # 每日配额（技术设计说明书 §8 准入条件第 6 条）——
    # 在任何模型调用之前拦，超限的请求一个 token 都不该花。
    quota = cfg.daily_quota
    if quota > 0:
        used = today_calls(cfg.audit_log)
        if used >= quota:
            tracer.add("quota", tracer.start(), f"当日已用 {used}/{quota}", status="blocked")
            return AskResult(
                ok=False, question=question, trace_id=trace_id, org_id=org,
                rejected_by="QUOTA",
                error=f"已达当日调用上限（{used}/{quota}）",
                hint="明日自动恢复；也可调高 config/askdb.yaml 的 observability.daily_quota。",
                steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
            )

    own_exec = executor is None
    ex = executor or Executor(cfg)
    deps = Deps(cfg=cfg, llm=llm or LlmClient(cfg), executor=ex, tracer=tracer)

    init: AskState = {
        "question": question, "org_id": org, "trace_id": trace_id,
        "attempt": 0, "max_retry": cfg.max_retry,
    }
    try:
        out = _GRAPH.invoke(
            init,
            {"recursion_limit": 40, "configurable": {"thread_id": trace_id, "deps": deps}},
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
        rejected_by=out.get("rejected_by"), error=out.get("error") or "",
        hint=out.get("error_hint", ""),
        tables_hit=out.get("tables_hit", []), metrics_hit=out.get("metrics_hit", []),
        attempts=out.get("attempt", 0) + 1,
        steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        tok_in=tok_in, tok_out=tok_out,
        cost_cny=cost_cny(tok_in, tok_out, cfg.llm),
    )

    write_audit(cfg.audit_log, {
        "trace_id": trace_id, "ts": now_iso(), "org_id": org, "question": question,
        "tables_hit": result.tables_hit, "metrics_hit": result.metrics_hit,
        "sql_raw": result.sql_raw, "sql_final": result.sql_final,
        "rules_fired": result.rules_fired, "rejected_by": result.rejected_by,
        "attempts": result.attempts, "explain_rows": out.get("explain_rows"),
        "rows_returned": result.row_count,
        "elapsed_ms": result.elapsed_ms, "tok_in": tok_in, "tok_out": tok_out,
        "cost_cny": result.cost_cny, "steps": result.steps,
    })
    return result
