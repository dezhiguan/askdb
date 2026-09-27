"""Review worker artifacts: protocol rules first, then a read-only react agent.

Missing Evidence, SQL or checksum is a repair without a model call. When every
subtask already has a complete artifact, the verifier spec may run check SQL
and add a repair of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from .budget import BudgetExceeded
from .evidence_store import latest_by_subtask
from .protocol import RepairTask, Review, ReviewVerdict


def verify(state: dict[str, Any]) -> tuple[Review, list[dict[str, Any]]]:
    active = latest_by_subtask(state.get("evidence_by_id") or {})
    tasks = state.get("subtasks_by_id") or {}
    issues: list[str] = []
    repairs: list[RepairTask] = []
    # 同一次返工轮次里也可能再核对（补派之后）。编号按已有 Review 递增，避免互相覆盖。
    visit = len(state.get("reviews_by_id") or {}) + 1
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
        review_id=f"{state['run_id']}:review:{visit}",
        run_id=state["run_id"], verdict=verdict,
        evidence_ids=[item["evidence_id"] for item in active.values()],
        issues=issues, repair_tasks=repairs,
        confidence=1.0 if not issues else 0.3,
    )
    return review, [item.model_dump(mode="json") for item in repairs]


class VerifierRepair(BaseModel):
    target_subtask_id: str
    reason: str
    instructions: str = "修正 SQL 并严格遵守统一语义契约后重新取证"


class VerifierOpinion(BaseModel):
    verdict: Literal["PASS", "REPAIR"] = "PASS"
    issues: list[str] = Field(default_factory=list)
    repairs: list[VerifierRepair] = Field(default_factory=list)


OPINION_SYSTEM = """你是 askdb Verifier。规则检查已经确认每份子任务都有 SQL 和校验和。
根据只读核对的结果，判断这些证据是否足以回答各自的子任务。
verdict 只能是 PASS 或 REPAIR。REPAIR 时 repairs 必须指向已有的 subtask_id，
并写清要改的口径。没有具体缺陷就 PASS。不要发明新的子任务，不要改数据。"""

VERIFIER_QUESTION = "核对每份证据是否足以回答对应子任务。只做只读检查，检查完成后结束。"


@dataclass
class CheckResult:
    review: Review
    repairs: list[dict[str, Any]]
    tokens: int = 0
    cost: float = 0.0
    halted: str = ""
    bindings: list[dict[str, Any]] = field(default_factory=list)
    kind: str = ""


def _spent(state: dict[str, Any]) -> int:
    return max(int(state.get("tok_used", 0)),
               sum(int(v) for v in (state.get("tok_by_actor") or {}).values()))


def _model(deps: Any, cfg: Any) -> Any:
    from ..agents.spec import load_agent
    from ..llm import LlmClient

    if deps.llm_factory:
        return deps.llm_factory(cfg)
    spec = load_agent(cfg, "verifier")
    if spec.model_explicit:
        return LlmClient(cfg, llm_cfg={**cfg.llm, "model": spec.model})
    return deps.llm


def _brief(state: dict[str, Any]) -> str:
    active = latest_by_subtask(state.get("evidence_by_id") or {})
    lines: list[str] = []
    for task_id, task in (state.get("subtasks_by_id") or {}).items():
        evidence = active.get(task_id) or {}
        rows = evidence.get("rows") or []
        lines.append(json.dumps({
            "subtask_id": task_id,
            "title": task.get("title", ""),
            "question": (task.get("query") or {}).get("question", ""),
            "sql_final": evidence.get("sql_final", ""),
            "columns": evidence.get("columns") or [],
            "rows": rows[:5],
            "row_count": evidence.get("row_count", 0),
            "checksum": evidence.get("checksum", ""),
        }, ensure_ascii=False, default=str))
    return "\n".join(lines)


def check(state: dict[str, Any], deps: Any) -> CheckResult:
    """Rules first. The react agent runs only after every artifact is complete."""
    review, repairs = verify(state)
    if repairs or not review.evidence_ids:
        return CheckResult(review, repairs)
    # 一份完整证据已经通过规则核对。再把子任务当新问题跑一遍召回和预检，
    # 只是把 Worker 已经写出的答案重查一次。
    active = latest_by_subtask(state.get("evidence_by_id") or {})
    tasks = state.get("subtasks_by_id") or {}
    if len(tasks) == 1 and len(active) == 1:
        only = next(iter(active.values()))
        if only.get("sql_final") and only.get("checksum"):
            return CheckResult(review, repairs, kind="rules")

    from ..agents.runner import run_react_inline
    from ..agents.spec import load_agent

    cfg = deps.config_for(state.get("source_id") or "")
    remaining = int(state["token_cap"]) - _spent(state)
    budget = deps.token_budget(state)
    if remaining < 1 or budget.exhausted():
        return CheckResult(review, repairs)

    client = _model(deps, cfg)
    spec = load_agent(cfg, "verifier")
    pinned = list((state.get("skill_bindings_by_role") or {}).get("verifier", []) or [])
    executor, own_executor = deps.executor_for(cfg)
    tracer = deps.tracer
    before_in = int(getattr(tracer, "tok_in", 0) or 0)
    before_out = int(getattr(tracer, "tok_out", 0) or 0)
    before_cost = float(getattr(tracer, "cost_cny", 0.0) or 0.0)
    reserve = min(remaining, 4_000)
    cost_reserve = float(deps.estimated_cost(reserve))
    try:
        budget.reserve(reserve, cost_reserve)
    except BudgetExceeded:
        if own_executor:
            executor.close()
        return CheckResult(review, repairs)

    parent = f"{state['run_id']}:supervisor"
    run_id = f"{state['run_id']}:verifier:{review.review_id.rsplit(':', 1)[-1]}"
    bindings: list[dict[str, Any]] = []
    answer = ""
    try:
        result, bound = run_react_inline(
            "verifier", VERIFIER_QUESTION, cfg, int(state.get("org_id", 0)),
            llm=client, executor=executor, tracer=tracer,
            agent_run_id=run_id, parent_agent_run_id=parent,
            context=_brief(state), pinned=pinned or None,
            cancel_check=deps.cancelled, max_steps=spec.max_steps,
            cost_cap=remaining,
        )
        bindings = [dict(item) for item in bound]
        answer = str(getattr(result, "answer", "") or "")
        if getattr(result, "rejected_by", None) == "CANCELED" or deps.cancelled():
            tokens = _delta(tracer, before_in, before_out)
            cost = _cost_delta(tracer, before_cost)
            budget.settle(reserve, tokens, cost_reserve, cost)
            return CheckResult(review, [], tokens, cost, halted="CANCELED",
                               bindings=bindings)
    except Exception as exc:
        tokens = _delta(tracer, before_in, before_out)
        cost = _cost_delta(tracer, before_cost)
        budget.settle(reserve, tokens, cost_reserve, cost)
        review.issues.append(f"只读核对没有完成：{exc}")
        return CheckResult(review, [], tokens, cost, bindings=bindings)
    finally:
        if own_executor:
            executor.close()

    tokens = _delta(tracer, before_in, before_out)
    cost = _cost_delta(tracer, before_cost)
    budget.settle(reserve, tokens, cost_reserve, cost)
    opinion, usage = _opinion(state, deps, client, review, answer)
    if usage is not None:
        tokens += int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0)
        cost += float(getattr(usage, "cost_cny", 0.0) or 0.0)
    if opinion is None or opinion.verdict != "REPAIR":
        return CheckResult(review, [], tokens, cost, bindings=bindings)

    known = set((state.get("subtasks_by_id") or {}))
    round_no = int(state.get("repair_round", 0)) + 1
    accepted: list[RepairTask] = []
    for item in opinion.repairs:
        if item.target_subtask_id not in known:
            continue
        accepted.append(RepairTask(
            repair_id=f"{state['run_id']}:repair:{round_no}:{item.target_subtask_id}",
            target_subtask_id=item.target_subtask_id,
            reason=item.reason or "只读核对未通过",
            instructions=item.instructions,
            round=round_no,
        ))
    if not accepted:
        return CheckResult(review, [], tokens, cost, bindings=bindings)
    issues = list(opinion.issues) or [item.reason for item in accepted]
    reviewed = Review(
        review_id=review.review_id, run_id=state["run_id"],
        verdict=ReviewVerdict.REPAIR,
        evidence_ids=list(review.evidence_ids),
        issues=issues, repair_tasks=accepted, confidence=0.4,
    )
    return CheckResult(
        reviewed, [item.model_dump(mode="json") for item in accepted],
        tokens, cost, bindings=bindings,
    )


def _delta(tracer: Any, before_in: int, before_out: int) -> int:
    return max(0, int(getattr(tracer, "tok_in", 0) or 0) - before_in
               + int(getattr(tracer, "tok_out", 0) or 0) - before_out)


def _cost_delta(tracer: Any, before_cost: float) -> float:
    return max(0.0, float(getattr(tracer, "cost_cny", 0.0) or 0.0) - before_cost)


def _opinion(state: dict[str, Any], deps: Any, client: Any, review: Review,
             answer: str) -> tuple[VerifierOpinion | None, Any]:
    human = json.dumps({
        "question": state.get("question", ""),
        "evidence_ids": review.evidence_ids,
        "check_notes": answer,
        "subtask_ids": list((state.get("subtasks_by_id") or {})),
    }, ensure_ascii=False, default=str)
    try:
        return deps.structured(state, VerifierOpinion, OPINION_SYSTEM, human, llm=client)
    except BudgetExceeded:
        return None, None
    except Exception as exc:
        review.issues.append(f"核对结论没有生成：{exc}")
        return None, None
