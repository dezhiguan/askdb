"""Checkpoint-safe state and reducers for the supervisor graph."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_by_id(left: dict[str, Any] | None,
                right: dict[str, Any] | None) -> dict[str, Any]:
    """Merge parallel worker writes without last-writer-wins data loss."""
    return {**(left or {}), **(right or {})}


class MultiAgentState(TypedDict, total=False):
    question: str
    run_id: str
    thread_id: str
    org_id: int
    source_id: str
    requested_mode: str
    phase: str
    plan: dict[str, Any]
    semantic_contract: dict[str, Any]
    scope_fingerprints: dict[str, str]
    join_contracts: list[dict[str, Any]]
    skill_bindings_by_role: Annotated[dict[str, list[dict[str, Any]]], merge_by_id]
    subtasks_by_id: Annotated[dict[str, dict[str, Any]], merge_by_id]
    evidence_by_id: Annotated[dict[str, dict[str, Any]], merge_by_id]
    worker_errors: Annotated[dict[str, dict[str, Any]], merge_by_id]
    tok_by_actor: Annotated[dict[str, int], merge_by_id]
    budget_blocks_by_actor: Annotated[dict[str, str], merge_by_id]
    cost_by_actor: Annotated[dict[str, float], merge_by_id]
    reviews_by_id: Annotated[dict[str, dict[str, Any]], merge_by_id]
    repair_round: int
    max_repair_rounds: int
    max_workers: int
    token_cap: int
    tok_used: int
    cost_cap_cny: float
    cost_used_cny: float
    answer: str
    claims: list[dict[str, Any]]
    status: str
    error: str
    # Ephemeral dispatch payloads are still declared because LangGraph filters
    # undeclared keys at node boundaries. They remain plain serializable data.
    worker_task: dict[str, Any]
    pending_repairs: list[dict[str, Any]]


def initial_state(*, question: str, run_id: str, thread_id: str, org_id: int,
                  source_id: str, requested_mode: str = "multi",
                  max_workers: int = 3, max_repair_rounds: int = 2,
                  token_cap: int = 30_000,
                  cost_cap_cny: float = 0.0,
                  scope_fingerprints: dict[str, str] | None = None,
                  join_contracts: list[dict[str, Any]] | None = None) -> MultiAgentState:
    return {
        "question": question,
        "run_id": run_id,
        "thread_id": thread_id,
        "org_id": org_id,
        "source_id": source_id,
        "requested_mode": requested_mode,
        "phase": "CREATED",
        "plan": {},
        "semantic_contract": {},
        "scope_fingerprints": dict(scope_fingerprints or {}),
        "join_contracts": list(join_contracts or []),
        "skill_bindings_by_role": {},
        "subtasks_by_id": {},
        "evidence_by_id": {},
        "worker_errors": {},
        "tok_by_actor": {},
        "budget_blocks_by_actor": {},
        "cost_by_actor": {},
        "reviews_by_id": {},
        "repair_round": 0,
        "max_repair_rounds": max_repair_rounds,
        "max_workers": max_workers,
        "token_cap": token_cap,
        "tok_used": 0,
        "cost_cap_cny": cost_cap_cny,
        "cost_used_cny": 0.0,
        "answer": "",
        "claims": [],
        "status": "CREATED",
        "error": "",
    }
