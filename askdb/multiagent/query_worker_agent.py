"""Query Worker: one call to the shared query agent, then an Evidence row."""

from __future__ import annotations

import json
from typing import Any

from .budget import BudgetExceeded
from .evidence_store import from_tool_result


def _spent(state: dict[str, Any]) -> int:
    return max(int(state.get("tok_used", 0)),
               sum(int(v) for v in (state.get("tok_by_actor") or {}).values()))


_LISTING_SHAPES = frozenset({"topn", "listing", "detail", "rank"})


def _task_scope(task: dict[str, Any]) -> dict[str, Any]:
    query = task.get("query") or {}
    tables = [str(name).strip() for name in
              ((query.get("constraints") or {}).get("tables") or [])
              if str(name).strip()]
    return {
        "tables": tables,
        "expected_shape": str(query.get("expected_shape") or "").strip().lower(),
    }


def _context(state: dict[str, Any], task: dict[str, Any]) -> str:
    parts: list[str] = []
    scope = _task_scope(task)
    if scope["tables"] or scope["expected_shape"]:
        parts.append("已确定的任务范围（优先使用这些表；列名以表结构为准，不要重写指标定义）：\n"
                     + json.dumps(scope, ensure_ascii=False))
    if scope["expected_shape"] in _LISTING_SHAPES:
        parts.append("这是一次取数就能回答的列举或 TopN。第一条成功返回结果行的查询就是答案，"
                     "不要再为并列规模或总量追加查询；取值相同就按已返回的行说明。")
    contract = state.get("semantic_contract") or {}
    if contract:
        parts.append("统一语义契约（所有子任务必须遵守）：\n"
                     + json.dumps(contract, ensure_ascii=False, default=str))
    repair = str(task.get("repair_instructions") or "").strip()
    if repair:
        parts.append("返工要求：\n" + repair)
    return "\n\n".join(parts)


def _tool_failure(result: Any) -> str:
    for step in reversed(result.steps or []):
        if step.get("tool") == "execute_sql" and step.get("status") not in ("ok", ""):
            code = str(step.get("error_code") or "")
            note = str(step.get("note") or step.get("error_message") or "")
            text = f"{code}: {note}".strip(": ").strip()
            if text:
                return text
    return result.error or "查询智能体没有产生可执行的 SQL"


def run_worker(state: dict[str, Any], deps: Any) -> dict[str, Any]:
    from ..agents.runner import run_react_inline

    task = state["worker_task"]
    task_id = task["subtask_id"]
    attempt = int(task.get("attempt", 0)) + 1
    source_id = task.get("source_id") or state["source_id"]
    cfg = deps.config_for(source_id)
    tracer = deps.tracer
    actor_key = f"{task_id}:attempt:{attempt}"
    parent = f"{state['run_id']}:supervisor"
    if deps.cancelled():
        return {"subtasks_by_id": {task_id: {
            **task, "status": "CANCELED", "attempt": attempt,
            "error": "任务已由发起人取消"}}}

    remaining = int(state["token_cap"]) - _spent(state)
    if remaining < 1 or deps.token_budget(state).exhausted():
        message = "Token 预算已耗尽，查询智能体未启动"
        return {
            "subtasks_by_id": {task_id: {**task, "status": "FAILED",
                                         "attempt": attempt, "error": message}},
            "worker_errors": {task_id: {
                "subtask_id": task_id, "attempt": attempt, "error": message}},
            "budget_blocks_by_actor": {actor_key: message},
        }

    question = task.get("query", {}).get("question") or state["question"]
    pinned = list((state.get("skill_bindings_by_role") or {}).get(
        f"query_worker:{task_id}", []) or [])
    executor, own_executor = deps.executor_for(cfg)
    before_in = int(getattr(tracer, "tok_in", 0) or 0)
    before_out = int(getattr(tracer, "tok_out", 0) or 0)
    before_cost = float(getattr(tracer, "cost_cny", 0.0) or 0.0)
    budget = deps.token_budget(state)
    reserve = min(remaining, 8_000)
    cost_reserve = float(deps.estimated_cost(reserve))
    try:
        budget.reserve(reserve, cost_reserve)
    except BudgetExceeded as exc:
        if own_executor:
            executor.close()
        return {
            "subtasks_by_id": {task_id: {**task, "status": "FAILED",
                                         "attempt": attempt, "error": str(exc)}},
            "worker_errors": {task_id: {
                "subtask_id": task_id, "attempt": attempt, "error": str(exc)}},
            "budget_blocks_by_actor": {actor_key: str(exc)},
        }

    try:
        result, bindings = run_react_inline(
            "query", question, cfg, int(state.get("org_id", 0)),
            llm=deps.llm_for(cfg), executor=executor, tracer=tracer,
            agent_run_id=task_id, parent_agent_run_id=parent,
            context=_context(state, task), task_scope=_task_scope(task),
            pinned=pinned or None,
            cancel_check=deps.cancelled, cost_cap=remaining,
        )
    except Exception as exc:  # the react loop's own failures are worker artifacts
        error = str(exc)
        if not any(getattr(step, "agent_run_id", "") == task_id
                   and getattr(step, "status", "") == "failed"
                   for step in getattr(tracer, "steps", [])):
            tracer.add("query_worker", tracer.start(), f"{task_id} 失败：{exc}",
                       status="failed", stage=task_id, input=task,
                       agent_run_id=task_id, agent_role="query_worker",
                       parent_agent_run_id=parent)
        tokens = max(0, int(getattr(tracer, "tok_in", 0) or 0) - before_in
                     + int(getattr(tracer, "tok_out", 0) or 0) - before_out)
        cost = max(0.0, float(getattr(tracer, "cost_cny", 0.0) or 0.0) - before_cost)
        budget.settle(reserve, tokens, cost_reserve, cost)
        return {
            "subtasks_by_id": {task_id: {**task, "status": "FAILED",
                                         "attempt": attempt, "error": error}},
            "worker_errors": {task_id: {
                "subtask_id": task_id, "attempt": attempt, "error": error}},
            "tok_by_actor": {actor_key: tokens},
            "cost_by_actor": {actor_key: cost},
            **({"budget_blocks_by_actor": {actor_key: error}}
               if isinstance(exc, BudgetExceeded) else {}),
        }
    finally:
        if own_executor:
            executor.close()

    tokens = max(0, int(getattr(tracer, "tok_in", 0) or 0) - before_in
                 + int(getattr(tracer, "tok_out", 0) or 0) - before_out)
    cost = max(0.0, float(getattr(tracer, "cost_cny", 0.0) or 0.0) - before_cost)
    budget.settle(reserve, tokens, cost_reserve, cost)
    usage = {"tok_by_actor": {actor_key: tokens}, "cost_by_actor": {actor_key: cost}}
    bound = [dict(item) for item in bindings]

    if getattr(result, "rejected_by", None) == "CANCELED" or deps.cancelled():
        return {"subtasks_by_id": {task_id: {
            **task, "status": "CANCELED", "attempt": attempt,
            "error": "任务已由发起人取消；SQL 未执行"}}, **usage}

    if not getattr(result, "sql_final", ""):
        error = _tool_failure(result)
        blocked = isinstance(error, str) and "预算" in error
        return {
            "subtasks_by_id": {task_id: {**task, "status": "FAILED",
                                         "attempt": attempt, "error": error}},
            "worker_errors": {task_id: {
                "subtask_id": task_id, "attempt": attempt, "error": error}},
            **usage,
            **({"budget_blocks_by_actor": {actor_key: error}} if blocked else {}),
        }

    evidence = from_tool_result(
        run_id=state["run_id"], subtask_id=task_id, source_id=source_id,
        data={
            "sql_final": result.sql_final,
            "columns": list(result.columns),
            "rows": list(result.rows),
            "row_count": result.row_count,
            "truncated": result.truncated,
            "as_of": result.as_of,
            "explain_rows": result.explain_rows,
            "rules_fired": list(result.rules_fired),
            "rewrites": list(result.rewrites),
            "masked_columns": list(result.masked_columns),
            "mask_degraded": result.mask_degraded,
        },
        bindings=bound,
        attempt=attempt,
        supersedes=str(task.get("previous_evidence_id", "")),
        scope_fingerprint=(state.get("scope_fingerprints") or {}).get(source_id, ""),
        semantic_contract=state.get("semantic_contract") or {},
        answer=str(getattr(result, "reasoning", "") or ""),
    )
    return {
        "subtasks_by_id": {task_id: {**task, "status": "SUCCEEDED",
                                     "attempt": attempt, "error": ""}},
        "evidence_by_id": {evidence.evidence_id: evidence.model_dump(mode="json")},
        "skill_bindings_by_role": {f"query_worker:{task_id}": bound},
        **usage,
    }
