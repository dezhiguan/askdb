"""Scoped Query Worker: schema recall → SQL generation → existing safe atom."""

from __future__ import annotations

import json
from typing import Any

from .. import skill, tools
from .budget import BudgetExceeded
from .evidence_store import from_tool_result


def run_worker(state: dict[str, Any], deps: Any) -> dict[str, Any]:
    task = state["worker_task"]
    task_id = task["subtask_id"]
    attempt = int(task.get("attempt", 0)) + 1
    source_id = task.get("source_id") or state["source_id"]
    cfg = deps.config_for(source_id)
    tracer = deps.tracer
    started = tracer.start()
    if deps.cancelled():
        return {"subtasks_by_id": {task_id: {
            **task, "status": "CANCELED", "attempt": attempt,
            "error": "任务已由发起人取消"}}}
    pinned = (state.get("skill_bindings_by_role") or {}).get(
        f"query_worker:{task_id}", [])
    resolved = skill.load_pinned(cfg, pinned) if pinned else skill.resolve(
        cfg, role="query_worker", source_id=source_id,
        question=task.get("query", {}).get("question") or state["question"],
        runtime_allowed_tools=tools.REGISTRY.keys(),
        agent_allowed_tools=("search_schema", "get_table_schema", "execute_sql"),
    )
    recall = tools.search_schema(
        task.get("query", {}).get("question") or state["question"], cfg)
    schema_prompt = str(recall.data.get("prompt", ""))
    contract = state.get("semantic_contract") or {}
    instructions = resolved.instructions()
    context = (
        schema_prompt
        + "\n\n【统一语义契约】\n"
        + json.dumps(contract, ensure_ascii=False, default=str)
        + ("\n\n【本 Worker Skills】\n- " + "\n- ".join(instructions)
           if instructions else "")
    )
    worker_llm = deps.llm_for(cfg)
    repair = str(task.get("repair_instructions", ""))
    usage = None
    actor_key = f"{task_id}:attempt:{attempt}"

    def tokens() -> dict[str, int]:
        count = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0)
        return {actor_key: count} if usage is not None else {}

    def cost() -> dict[str, float]:
        return {actor_key: float(getattr(usage, "cost_cny", 0.0) or 0.0)} \
            if usage is not None else {}

    try:
        draft, usage = deps.worker_sql(state, worker_llm,
            task.get("query", {}).get("question") or state["question"],
            context,
            dialect=cfg.dialect,
            error=repair,
            step=task.get("title", ""),
        )
        sql = str(getattr(draft, "sql", ""))
        if deps.token_budget(state).exhausted():
            raise BudgetExceeded("Worker 生成 SQL 后 Token 预算已耗尽，SQL 未执行")
        if not sql:
            raise ValueError(getattr(draft, "reasoning", "Worker 未生成 SQL"))
        if deps.cancelled():
            return {"subtasks_by_id": {task_id: {
                **task, "status": "CANCELED", "attempt": attempt,
                "error": "任务已由发起人取消；SQL 未执行"}},
                "tok_by_actor": tokens(), "cost_by_actor": cost()}
        executor, own_executor = deps.executor_for(cfg)
        try:
            result = tools.execute_sql(sql, cfg, int(state.get("org_id", 0)), executor)
        finally:
            if own_executor:
                executor.close()
        if not result.ok:
            raise ValueError(f"{result.rejected_by or 'EXEC'}: {result.error}")
        previous = str(task.get("previous_evidence_id", ""))
        evidence = from_tool_result(
            run_id=state["run_id"], subtask_id=task_id, source_id=source_id,
            data=result.data,
            bindings=[item.model_dump(mode="json") for item in resolved.bindings],
            attempt=attempt,
            supersedes=previous,
            scope_fingerprint=(state.get("scope_fingerprints") or {}).get(source_id, ""),
            semantic_contract=contract,
        )
        updated = {**task, "status": "SUCCEEDED", "attempt": attempt, "error": ""}
        tracer.add(
            "query_worker", started, f"{task_id} 产出 {evidence.row_count} 行证据",
            tok_in=getattr(usage, "input_tokens", 0),
            tok_out=getattr(usage, "output_tokens", 0),
            cost_cny=getattr(usage, "cost_cny", 0.0),
            tables=list(recall.data.get("tables", [])), stage=task_id,
            input=task, output={
                "evidence_id": evidence.evidence_id,
                "checksum": evidence.checksum,
                "skill_bindings": [item.model_dump(mode="json")
                                   for item in resolved.bindings]},
        )
        return {
            "subtasks_by_id": {task_id: updated},
            "evidence_by_id": {evidence.evidence_id: evidence.model_dump(mode="json")},
            "skill_bindings_by_role": {
                f"query_worker:{task_id}": [item.model_dump(mode="json")
                                             for item in resolved.bindings]},
            "tok_by_actor": tokens(),
            "cost_by_actor": cost(),
        }
    except Exception as exc:  # worker failures are artifacts, not graph crashes
        updated = {**task, "status": "FAILED", "attempt": attempt, "error": str(exc)}
        tracer.add("query_worker", started, f"{task_id} 失败：{exc}",
                   status="failed", stage=task_id, input=task)
        return {
            "subtasks_by_id": {task_id: updated},
            "worker_errors": {task_id: {
                "subtask_id": task_id, "attempt": attempt, "error": str(exc)}},
            "tok_by_actor": tokens(),
            "cost_by_actor": cost(),
            **({"budget_blocks_by_actor": {actor_key: str(exc)}}
               if isinstance(exc, BudgetExceeded) else {}),
        }
