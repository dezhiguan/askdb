"""Runtime limits for dynamic plans."""

from __future__ import annotations

from .protocol import TaskPlan


class PlanPolicyError(ValueError):
    pass


def enforce_plan(plan: TaskPlan, *, max_workers: int, token_cap: int) -> None:
    workers = [task for task in plan.subtasks if task.assigned_role == "query_worker"]
    if not workers:
        raise PlanPolicyError("supervisor plan contains no query worker")
    if len(workers) > max_workers:
        raise PlanPolicyError(
            f"supervisor requested {len(workers)} workers; limit is {max_workers}")
    if plan.budget.token_cap > token_cap:
        raise PlanPolicyError(
            f"plan token budget {plan.budget.token_cap} exceeds runtime cap {token_cap}")
