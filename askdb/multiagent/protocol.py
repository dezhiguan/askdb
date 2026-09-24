"""Serializable contracts shared by every agent in a run.

Agents exchange these records through graph state instead of conversational
messages.  This makes fan-out/fan-in deterministic, gives the verifier concrete
inputs and lets a checkpoint resume from the same execution path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class AgentRole(str, Enum):
    SUPERVISOR = "supervisor"
    SEMANTIC = "semantic"
    QUERY_WORKER = "query_worker"
    VERIFIER = "verifier"
    SYNTHESIZER = "synthesizer"


class SubTaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    RECOVERING = "RECOVERING"


class ReviewVerdict(str, Enum):
    PASS = "PASS"
    REPAIR = "REPAIR"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"
    NEED_HUMAN = "NEED_HUMAN"


class Budget(_Contract):
    max_steps: int = Field(default=6, ge=1)
    max_workers: int = Field(default=4, ge=1)
    max_repair_rounds: int = Field(default=1, ge=0)
    token_cap: int = Field(default=12_000, ge=0)
    timeout_seconds: float = Field(default=120.0, gt=0)


class SkillBinding(_Contract):
    skill_id: str
    version: str
    checksum: str = ""
    selection_reason: str = ""
    effective_tools: list[str] = Field(default_factory=list)
    config_hash: str = ""
    source: str = "registry"


class QuerySpec(_Contract):
    question: str
    source_id: str
    semantic_context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)


class SubTask(_Contract):
    subtask_id: str
    title: str
    assigned_role: AgentRole
    source_id: str = ""
    depends_on: list[str] = Field(default_factory=list)
    query: QuerySpec | None = None
    status: SubTaskStatus = SubTaskStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    error: str = ""


class TaskPlan(_Contract):
    plan_id: str
    run_id: str
    question: str
    execution_mode: str = "multi"
    budget: Budget = Field(default_factory=Budget)
    subtasks: list[SubTask] = Field(default_factory=list)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="after")
    def validate_dag(self) -> "TaskPlan":
        ids = [task.subtask_id for task in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("subtask_id must be unique within a plan")
        known = set(ids)
        graph: dict[str, list[str]] = {}
        for task in self.subtasks:
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(
                    f"subtask {task.subtask_id} depends on unknown tasks: {sorted(missing)}")
            if task.subtask_id in task.depends_on:
                raise ValueError(f"subtask {task.subtask_id} cannot depend on itself")
            graph[task.subtask_id] = list(task.depends_on)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("subtask dependencies contain a cycle")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for task_id in ids:
            visit(task_id)
        return self


class Evidence(_Contract):
    evidence_id: str
    run_id: str
    task_id: str
    subtask_id: str
    agent_role: AgentRole = AgentRole.QUERY_WORKER
    source_id: str
    sql_final: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    truncated: bool = False
    as_of: str = ""
    explain_rows: int | None = None
    rules_fired: list[str] = Field(default_factory=list)
    rewrites: list[str] = Field(default_factory=list)
    masked_columns: list[str] = Field(default_factory=list)
    mask_degraded: bool = False
    skill_bindings: list[SkillBinding] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    checksum: str = ""
    supersedes: str = ""

    @model_validator(mode="after")
    def fill_checksum(self) -> "Evidence":
        if self.checksum:
            return self
        payload = self.model_dump(mode="json", exclude={"checksum"})
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
        self.checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
        return self


class RepairTask(_Contract):
    repair_id: str
    target_subtask_id: str
    reason: str
    instructions: str = ""
    round: int = Field(default=1, ge=1)


class Review(_Contract):
    review_id: str
    run_id: str
    verdict: ReviewVerdict
    evidence_ids: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    repair_tasks: list[RepairTask] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)


class Claim(_Contract):
    claim_id: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)
    caveats: list[str] = Field(default_factory=list)


class MultiAgentArtifacts(_Contract):
    execution_mode: str
    plan: TaskPlan
    evidence: list[Evidence] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    skill_bindings: list[SkillBinding] = Field(default_factory=list)

    def as_result_fields(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode,
            "plan": self.plan.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
            "reviews": [item.model_dump(mode="json") for item in self.reviews],
            "claims": [item.model_dump(mode="json") for item in self.claims],
            "skill_bindings": [item.model_dump(mode="json") for item in self.skill_bindings],
        }


def single_agent_artifacts(
    *, question: str, trace_id: str, source_id: str,
    exec_results: list[dict[str, Any]], answer: str,
) -> MultiAgentArtifacts:
    """Project the existing single-agent run into the new artifact contract.

    Keeping this adapter from day one means API consumers can adopt the evidence
    model before dynamic orchestration is enabled.  It also provides the baseline
    used to compare shadow multi-agent runs.
    """
    successful = [row for row in exec_results if row and row.get("sql_final")]
    status = SubTaskStatus.SUCCEEDED if successful else SubTaskStatus.FAILED
    subtask_id = f"{trace_id}:query:0"
    plan = TaskPlan(
        plan_id=f"{trace_id}:plan", run_id=trace_id, question=question,
        execution_mode="single",
        subtasks=[SubTask(
            subtask_id=subtask_id,
            title="执行数据查询",
            assigned_role=AgentRole.QUERY_WORKER,
            source_id=source_id,
            query=QuerySpec(question=question, source_id=source_id),
            status=status,
            attempt=max(1, len(exec_results)),
            error="" if successful else "未产生可用查询证据",
        )],
    )
    evidence = [
        Evidence(
            evidence_id=f"{trace_id}:evidence:{index}",
            run_id=trace_id,
            task_id=trace_id,
            subtask_id=subtask_id,
            source_id=source_id,
            sql_final=str(row.get("sql_final", "")),
            columns=list(row.get("columns", [])),
            rows=list(row.get("rows", [])),
            row_count=int(row.get("row_count", 0)),
            truncated=bool(row.get("truncated", False)),
            as_of=str(row.get("as_of", "")),
            explain_rows=row.get("explain_rows"),
            rules_fired=list(row.get("rules_fired", [])),
            rewrites=list(row.get("rewrites", [])),
            masked_columns=list(row.get("masked_columns", [])),
            mask_degraded=bool(row.get("mask_degraded", False)),
        )
        for index, row in enumerate(successful)
    ]
    evidence_ids = [item.evidence_id for item in evidence]
    reviews = []
    if evidence_ids:
        reviews.append(Review(
            review_id=f"{trace_id}:review:0", run_id=trace_id,
            verdict=ReviewVerdict.PASS, evidence_ids=evidence_ids,
        ))
    claims = []
    if answer:
        claims.append(Claim(
            claim_id=f"{trace_id}:claim:0", text=answer,
            evidence_ids=evidence_ids,
            confidence=1.0 if evidence_ids else 0.25,
            caveats=[] if evidence_ids else ["本次回答没有可绑定的查询证据"],
        ))
    return MultiAgentArtifacts(
        execution_mode="single", plan=plan, evidence=evidence,
        reviews=reviews, claims=claims,
    )
