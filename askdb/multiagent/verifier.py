"""Independent deterministic review of worker artifacts."""

from __future__ import annotations

from typing import Any

from .evidence_store import latest_by_subtask
from .protocol import RepairTask, Review, ReviewVerdict


def verify(state: dict[str, Any]) -> tuple[Review, list[dict[str, Any]]]:
    active = latest_by_subtask(state.get("evidence_by_id") or {})
    tasks = state.get("subtasks_by_id") or {}
    issues: list[str] = []
    repairs: list[RepairTask] = []
    round_no = int(state.get("repair_round", 0)) + 1
    for task_id, task in tasks.items():
        evidence = active.get(task_id)
        if evidence is None:
            reason = (state.get("worker_errors") or {}).get(task_id, {}).get(
                "error", "没有产生 Evidence")
            issues.append(f"{task_id}: {reason}")
            repairs.append(RepairTask(
                repair_id=f"{state['run_id']}:repair:{round_no}:{task_id}",
                target_subtask_id=task_id,
                reason=reason,
                instructions="修正 SQL 并严格遵守统一语义契约后重新取证",
                round=round_no,
            ))
            continue
        if not evidence.get("sql_final") or not evidence.get("checksum"):
            issues.append(f"{task_id}: 证据缺少 SQL 或校验和")
            repairs.append(RepairTask(
                repair_id=f"{state['run_id']}:repair:{round_no}:{task_id}",
                target_subtask_id=task_id,
                reason="证据协议不完整",
                instructions="重新执行并返回完整 Evidence",
                round=round_no,
            ))
    verdict = ReviewVerdict.REPAIR if repairs else ReviewVerdict.PASS
    review = Review(
        review_id=f"{state['run_id']}:review:{round_no}",
        run_id=state["run_id"], verdict=verdict,
        evidence_ids=[item["evidence_id"] for item in active.values()],
        issues=issues, repair_tasks=repairs,
        confidence=1.0 if not issues else 0.3,
    )
    return review, [item.model_dump(mode="json") for item in repairs]
