"""Top-level LangGraph for evidence-first multi-agent orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from .. import skill, tools
from ..config import Config
from ..executor import Executor
from ..llm import LlmClient
from ..trace import Tracer
from .policy import enforce_plan
from .protocol import (
    AgentRole,
    Budget,
    Claim,
    QuerySpec,
    Review,
    ReviewVerdict,
    SubTask,
    TaskPlan,
)
from .query_worker import run_worker
from .semantic_agent import SYSTEM as SEMANTIC_SYSTEM, SemanticContractDraft
from .state import MultiAgentState
from .synthesizer import SYSTEM as SYNTHESIS_SYSTEM, SynthesisDraft
from .verifier import verify


class PlannedAnalysis(BaseModel):
    title: str
    question: str
    source_id: str = ""
    depends_on: list[int] = Field(default_factory=list)


class SupervisorPlanDraft(BaseModel):
    reasoning: str = ""
    analyses: list[PlannedAnalysis] = Field(min_length=1, max_length=12)


SUPERVISOR_SYSTEM = """你是 askdb Supervisor。把复杂数据问题拆成最少量、可独立取证的
查询子任务。每个子任务必须回答原问题的一部分，不能把一个 SQL 能完成的工作重复拆分。
同一分析维度只创建一个子任务；depends_on 使用 analyses 的零基下标。不要生成 SQL。"""


@dataclass
class MultiAgentDeps:
    cfg: Config
    llm: Any
    tracer: Tracer
    source_configs: dict[str, Config] | None = None
    llm_factory: Callable[[Config], Any] | None = None
    executor_factory: Callable[[Config], Any] | None = None

    def config_for(self, source_id: str) -> Config:
        if self.source_configs and source_id in self.source_configs:
            return self.source_configs[source_id]
        if source_id not in ("", "builtin", self.cfg.source_id or "builtin"):
            raise ValueError(f"数据源未获本次运行授权：{source_id}")
        return self.cfg

    def llm_for(self, cfg: Config) -> Any:
        return self.llm_factory(cfg) if self.llm_factory else LlmClient(cfg)

    def executor_for(self, cfg: Config) -> tuple[Any, bool]:
        if self.executor_factory:
            return self.executor_factory(cfg), True
        return Executor(cfg), True


def _deps(config: RunnableConfig) -> MultiAgentDeps:
    return config["configurable"]["deps"]


def _usage_kwargs(usage: Any) -> dict[str, Any]:
    return {
        "tok_in": int(getattr(usage, "input_tokens", 0) or 0),
        "tok_out": int(getattr(usage, "output_tokens", 0) or 0),
        "cost_cny": float(getattr(usage, "cost_cny", 0.0) or 0.0),
    }


def _supervisor(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    started = deps.tracer.start()
    draft, usage = deps.llm.structured(
        SupervisorPlanDraft, SUPERVISOR_SYSTEM,
        f"用户问题：{state['question']}\n默认数据源：{state['source_id']}\n"
        f"最多创建 {state['max_workers']} 个查询子任务。",
    )
    analyses = list(draft.analyses)[:int(state["max_workers"])]
    tasks: list[SubTask] = []
    for index, item in enumerate(analyses):
        source_id = item.source_id or state["source_id"]
        dependencies = [f"{state['run_id']}:worker:{dep}" for dep in item.depends_on
                        if 0 <= dep < len(analyses)]
        tasks.append(SubTask(
            subtask_id=f"{state['run_id']}:worker:{index}",
            title=item.title,
            assigned_role=AgentRole.QUERY_WORKER,
            source_id=source_id,
            depends_on=dependencies,
            query=QuerySpec(question=item.question, source_id=source_id),
        ))
    plan = TaskPlan(
        plan_id=f"{state['run_id']}:plan",
        run_id=state["run_id"], question=state["question"],
        budget=Budget(
            max_workers=int(state["max_workers"]),
            max_repair_rounds=int(state["max_repair_rounds"]),
            token_cap=int(state["token_cap"]),
        ),
        subtasks=tasks,
    )
    enforce_plan(plan, max_workers=int(state["max_workers"]),
                 token_cap=int(state["token_cap"]))
    deps.tracer.add("supervisor", started, draft.reasoning or f"拆分为 {len(tasks)} 个子任务",
                    stage="supervisor", input=state["question"],
                    output=plan.model_dump(mode="json"), **_usage_kwargs(usage))
    return {
        "phase": "PLANNING",
        "status": "PLANNING",
        "plan": plan.model_dump(mode="json"),
        "subtasks_by_id": {
            task.subtask_id: task.model_dump(mode="json") for task in tasks},
        "tok_used": int(state.get("tok_used", 0))
                    + int(getattr(usage, "input_tokens", 0) or 0)
                    + int(getattr(usage, "output_tokens", 0) or 0),
    }


def _resolve_roles(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    started = deps.tracer.start()
    roles = ("semantic", "verifier", "synthesizer")
    bindings: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        report = skill.resolve(
            deps.cfg, role=role, source_id=state["source_id"],
            question=state["question"], runtime_allowed_tools=tools.REGISTRY.keys(),
            agent_allowed_tools=(),
        )
        bindings[role] = [item.model_dump(mode="json") for item in report.bindings]
    deps.tracer.add("resolve_skills", started,
                    f"为 {len(roles)} 个角色固定 Skill 版本", stage="runtime",
                    output=bindings)
    return {"skill_bindings_by_role": bindings}


def _semantic(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    started = deps.tracer.start()
    bindings = (state.get("skill_bindings_by_role") or {}).get("semantic", [])
    report = skill.resolve(
        deps.cfg, role="semantic", source_id=state["source_id"],
        question=state["question"])
    instructions = report.instructions()
    human = (
        f"用户问题：{state['question']}\n"
        f"任务计划：{json.dumps(state['plan'], ensure_ascii=False)}\n"
        + ("已绑定方法：\n- " + "\n- ".join(instructions) if instructions else "")
    )
    contract, usage = deps.llm.structured(SemanticContractDraft, SEMANTIC_SYSTEM, human)
    payload = contract.model_dump(mode="json")
    tasks: dict[str, dict[str, Any]] = {}
    for task_id, task in (state.get("subtasks_by_id") or {}).items():
        query = dict(task.get("query") or {})
        query["semantic_context"] = payload
        tasks[task_id] = {**task, "query": query}
    deps.tracer.add("semantic", started, contract.metric_definition,
                    stage="semantic", input=state["plan"], output=payload,
                    **_usage_kwargs(usage))
    return {
        "phase": "DISPATCHING", "status": "DISPATCHING",
        "semantic_contract": payload,
        "subtasks_by_id": tasks,
        "tok_used": int(state.get("tok_used", 0))
                    + int(getattr(usage, "input_tokens", 0) or 0)
                    + int(getattr(usage, "output_tokens", 0) or 0),
        "skill_bindings_by_role": {"semantic": bindings},
    }


def _dispatch_gate(state: MultiAgentState) -> dict[str, Any]:
    return {"phase": "DISPATCHING", "status": "RUNNING"}


def _dispatch(state: MultiAgentState) -> list[Send] | str:
    tasks = list((state.get("subtasks_by_id") or {}).values())
    by_id = state.get("subtasks_by_id") or {}
    ready = [task for task in tasks
             if task.get("status") in ("PENDING", "RECOVERING")
             and all((by_id.get(dependency) or {}).get("status") == "SUCCEEDED"
                     for dependency in task.get("depends_on", []))]
    if not ready:
        return "verifier"
    return [Send("query_worker", {
        **state,
        "phase": "RUNNING",
        "worker_task": task,
    }) for task in ready]


def _worker(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    return run_worker(state, _deps(config))


def _verifier(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    started = deps.tracer.start()
    review, repairs = verify(state)
    current_round = int(state.get("repair_round", 0))
    exhausted = bool(repairs) and current_round >= int(state["max_repair_rounds"])
    if exhausted:
        review.verdict = ReviewVerdict.INSUFFICIENT.value
        review.issues.append("已达到最大返工轮次，按现有证据降级收敛")
        review.repair_tasks = []
        repairs = []
    deps.tracer.add("verifier", started,
                    f"{review.verdict}：{len(review.evidence_ids)} 份证据",
                    status="degraded" if review.verdict != "PASS" else "ok",
                    stage="verifier", output=review.model_dump(mode="json"))
    return {
        "phase": "VERIFYING", "status": "VERIFYING",
        "reviews_by_id": {review.review_id: review.model_dump(mode="json")},
        "pending_repairs": repairs,
        "repair_round": current_round + (1 if repairs else 0),
    }


def _after_verify(state: MultiAgentState) -> list[Send] | str:
    repairs = state.get("pending_repairs") or []
    if not repairs:
        return "synthesizer"
    sends: list[Send] = []
    evidence = state.get("evidence_by_id") or {}
    for repair in repairs:
        task_id = repair["target_subtask_id"]
        task = dict((state.get("subtasks_by_id") or {})[task_id])
        previous = next((item["evidence_id"] for item in evidence.values()
                         if item.get("subtask_id") == task_id and not item.get("supersedes")), "")
        task.update({
            "status": "RECOVERING",
            "repair_instructions": repair.get("instructions", ""),
            "previous_evidence_id": previous,
        })
        sends.append(Send("query_worker", {**state, "worker_task": task,
                                             "phase": "RECOVERING"}))
    return sends


def _synthesizer(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    started = deps.tracer.start()
    reviews = list((state.get("reviews_by_id") or {}).values())
    latest_review = reviews[-1] if reviews else {}
    approved_ids = set(latest_review.get("evidence_ids") or [])
    evidence = [item for key, item in (state.get("evidence_by_id") or {}).items()
                if key in approved_ids]
    human = json.dumps({
        "question": state["question"],
        "semantic_contract": state.get("semantic_contract") or {},
        "review": latest_review,
        "evidence": evidence,
    }, ensure_ascii=False, default=str)
    draft, usage = deps.llm.structured(SynthesisDraft, SYNTHESIS_SYSTEM, human)
    caveats = list(draft.caveats)
    if latest_review.get("verdict") != "PASS":
        caveats.extend(latest_review.get("issues") or [])
    claim_texts = list(draft.claims) or ([draft.answer] if draft.answer else [])
    claims = [Claim(
        claim_id=f"{state['run_id']}:claim:{index}", text=text,
        evidence_ids=sorted(approved_ids),
        confidence=float(latest_review.get("confidence", 0.5)),
        caveats=caveats,
    ).model_dump(mode="json") for index, text in enumerate(claim_texts)]
    # Claim gate: a positive-confidence claim may not escape without evidence.
    for claim in claims:
        if claim["confidence"] > 0.25 and not claim["evidence_ids"]:
            claim["confidence"] = 0.25
            claim["caveats"].append("没有通过验证的 Evidence 可绑定")
    deps.tracer.add("synthesizer", started, f"合成 {len(claims)} 条 Claim",
                    stage="synthesizer", input=latest_review,
                    output={"answer": draft.answer, "claims": claims},
                    **_usage_kwargs(usage))
    return {
        "phase": "COMPLETED", "status": "COMPLETED",
        "answer": draft.answer, "claims": claims,
        "tok_used": int(state.get("tok_used", 0))
                    + int(getattr(usage, "input_tokens", 0) or 0)
                    + int(getattr(usage, "output_tokens", 0) or 0),
    }


def build_graph(*, checkpointer: Any = None):
    graph = StateGraph(MultiAgentState)
    graph.add_node("supervisor", _supervisor)
    graph.add_node("resolve_skills", _resolve_roles)
    graph.add_node("semantic", _semantic)
    graph.add_node("dispatch_gate", _dispatch_gate)
    graph.add_node("query_worker", _worker)
    graph.add_node("verifier", _verifier)
    graph.add_node("synthesizer", _synthesizer)
    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", "resolve_skills")
    graph.add_edge("resolve_skills", "semantic")
    graph.add_edge("semantic", "dispatch_gate")
    graph.add_conditional_edges("dispatch_gate", _dispatch, ["query_worker", "verifier"])
    graph.add_edge("query_worker", "dispatch_gate")
    graph.add_conditional_edges("verifier", _after_verify,
                                ["query_worker", "synthesizer"])
    graph.add_edge("synthesizer", END)
    return graph.compile(checkpointer=checkpointer)
