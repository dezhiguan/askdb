"""Top-level LangGraph for evidence-first multi-agent orchestration."""

from __future__ import annotations

import json
import threading
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
from .budget import BudgetExceeded, TokenBudget, estimate_tokens
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
    cancel_event: threading.Event | None = None
    cancel_check: Callable[[], bool] | None = None
    budget: TokenBudget | None = None
    budget_lock: threading.Lock = None  # initialized below for test-created deps

    def __post_init__(self) -> None:
        self.budget_lock = threading.Lock()

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

    def cancelled(self) -> bool:
        return bool((self.cancel_event and self.cancel_event.is_set())
                    or (self.cancel_check and self.cancel_check()))

    def token_budget(self, state: MultiAgentState) -> TokenBudget:
        with self.budget_lock:
            if self.budget is None:
                self.budget = TokenBudget(int(state["token_cap"]),
                                          int(state.get("tok_used", 0)),
                                          cost_cap_cny=float(state.get("cost_cap_cny", 0)),
                                          cost_spent_cny=float(state.get("cost_used_cny", 0)))
            return self.budget

    def estimated_cost(self, tokens: int) -> float:
        prices = [self.cfg.llm]
        fallback = self.cfg.llm.get("fallback")
        if isinstance(fallback, dict):
            prices.append(fallback)
        maximum = max(float(item.get(key, 0) or 0)
                      for item in prices for key in
                      ("price_input_per_1k", "price_output_per_1k"))
        return tokens / 1000 * maximum

    def structured(self, state: MultiAgentState, schema: Any,
                   system: str, human: str) -> tuple[Any, Any]:
        budget = self.token_budget(state)
        reserved = estimate_tokens(system, human, schema.model_json_schema())
        cost_reserved = self.estimated_cost(reserved)
        budget.reserve(reserved, cost_reserved)
        try:
            result, usage = self.llm.structured(schema, system, human)
            budget.settle(reserved, _used_tokens(usage), cost_reserved,
                          _used_cost(usage))
            return result, usage
        except BaseException:
            budget.settle(reserved, 0, cost_reserved)
            raise

    def worker_sql(self, state: MultiAgentState, llm: Any,
                   *args: Any, **kwargs: Any) -> tuple[Any, Any]:
        budget = self.token_budget(state)
        # Worker prompt includes a large built-in SQL policy in LlmClient.
        from ..llm import SYSTEM, SqlDraft

        reserved = estimate_tokens(SYSTEM, args, kwargs, SqlDraft.model_json_schema())
        cost_reserved = self.estimated_cost(reserved)
        budget.reserve(reserved, cost_reserved)
        try:
            result, usage = llm.generate_sql(*args, **kwargs)
            budget.settle(reserved, _used_tokens(usage), cost_reserved,
                          _used_cost(usage))
            return result, usage
        except BaseException:
            budget.settle(reserved, 0, cost_reserved)
            raise


def _deps(config: RunnableConfig) -> MultiAgentDeps:
    return config["configurable"]["deps"]


def _usage_kwargs(usage: Any) -> dict[str, Any]:
    return {
        "tok_in": int(getattr(usage, "input_tokens", 0) or 0),
        "tok_out": int(getattr(usage, "output_tokens", 0) or 0),
        "cost_cny": float(getattr(usage, "cost_cny", 0.0) or 0.0),
    }


def _used_tokens(usage: Any) -> int:
    return int(getattr(usage, "input_tokens", 0) or 0) + int(
        getattr(usage, "output_tokens", 0) or 0)


def _used_cost(usage: Any) -> float:
    return float(getattr(usage, "cost_cny", 0.0) or 0.0)


def _total(state: MultiAgentState) -> int:
    return max(int(state.get("tok_used", 0)),
               sum((state.get("tok_by_actor") or {}).values()))


def _total_cost(state: MultiAgentState) -> float:
    return max(float(state.get("cost_used_cny", 0)),
               sum((state.get("cost_by_actor") or {}).values()))


def _budget_stop(state: MultiAgentState, deps: MultiAgentDeps) -> bool:
    return bool(state.get("budget_blocks_by_actor")) or _total(state) >= int(
        state["token_cap"]) or deps.token_budget(state).exhausted()


def _supervisor(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    started = deps.tracer.start()
    if deps.cancelled():
        return {"phase": "CANCELED", "status": "CANCELED"}
    try:
        draft, usage = deps.structured(
            state, SupervisorPlanDraft, SUPERVISOR_SYSTEM,
            f"用户问题：{state['question']}\n默认数据源：{state['source_id']}\n"
            f"本次已授权数据源：{sorted((deps.source_configs or {state['source_id']: deps.cfg}).keys())}\n"
            f"最多创建 {state['max_workers']} 个查询子任务。",
        )
    except BudgetExceeded as exc:
        return {"phase": "BUDGET_EXCEEDED", "status": "BUDGET_EXCEEDED",
                "error": str(exc), "budget_blocks_by_actor": {"supervisor": str(exc)}}
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
            cost_cap_cny=float(state.get("cost_cap_cny", 0)),
        ),
        subtasks=tasks,
    )
    enforce_plan(plan, max_workers=int(state["max_workers"]),
                 token_cap=int(state["token_cap"]))
    # Supervisor 只能从 Runtime 已收窄的配置中选源，不能凭模型输出扩权。
    for task in tasks:
        deps.config_for(task.source_id or state["source_id"])
    deps.tracer.add("supervisor", started, draft.reasoning or f"拆分为 {len(tasks)} 个子任务",
                    stage="supervisor", input=state["question"],
                    output=plan.model_dump(mode="json"), **_usage_kwargs(usage))
    return {
        "phase": "PLANNING",
        "status": "PLANNING",
        "plan": plan.model_dump(mode="json"),
        "subtasks_by_id": {
            task.subtask_id: task.model_dump(mode="json") for task in tasks},
        "tok_used": _total(state) + _used_tokens(usage),
        "tok_by_actor": {"supervisor": _used_tokens(usage)},
        "cost_used_cny": _total_cost(state) + _used_cost(usage),
        "cost_by_actor": {"supervisor": _used_cost(usage)},
    }


def _resolve_roles(state: MultiAgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    if state.get("status") in ("CANCELED", "BUDGET_EXCEEDED") or deps.cancelled():
        return {}
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
    if state.get("status") == "CANCELED" or deps.cancelled():
        return {"phase": "CANCELED", "status": "CANCELED"}
    if _budget_stop(state, deps):
        return {"phase": "BUDGET_EXCEEDED", "status": "BUDGET_EXCEEDED",
                "error": "Token 预算已耗尽，未执行后续模型调用"}
    started = deps.tracer.start()
    bindings = (state.get("skill_bindings_by_role") or {}).get("semantic", [])
    report = skill.load_pinned(deps.cfg, bindings)
    instructions = report.instructions()
    human = (
        f"用户问题：{state['question']}\n"
        f"任务计划：{json.dumps(state['plan'], ensure_ascii=False)}\n"
        f"批准的跨源契约：{json.dumps(state.get('join_contracts') or [], ensure_ascii=False)}\n"
        + ("已绑定方法：\n- " + "\n- ".join(instructions) if instructions else "")
    )
    try:
        contract, usage = deps.structured(
            state, SemanticContractDraft, SEMANTIC_SYSTEM, human)
    except BudgetExceeded as exc:
        return {"phase": "BUDGET_EXCEEDED", "status": "BUDGET_EXCEEDED",
                "error": str(exc), "budget_blocks_by_actor": {"semantic": str(exc)}}
    payload = contract.model_dump(mode="json")
    tasks: dict[str, dict[str, Any]] = {}
    for task_id, task in (state.get("subtasks_by_id") or {}).items():
        query = dict(task.get("query") or {})
        query["semantic_context"] = payload
        tasks[task_id] = {**task, "query": query}
    deps.tracer.add("semantic", started, contract.metric_definition,
                    stage="semantic", input=state["plan"],
                    output={"contract": payload, "skill_bindings": bindings},
                    **_usage_kwargs(usage))
    return {
        "phase": "DISPATCHING", "status": "DISPATCHING",
        "semantic_contract": payload,
        "subtasks_by_id": tasks,
        "tok_used": _total(state) + _used_tokens(usage),
        "tok_by_actor": {"semantic": _used_tokens(usage)},
        "cost_used_cny": _total_cost(state) + _used_cost(usage),
        "cost_by_actor": {"semantic": _used_cost(usage)},
        "skill_bindings_by_role": {"semantic": bindings},
    }


def _dispatch_gate(state: MultiAgentState) -> dict[str, Any]:
    if state.get("status") in ("CANCELED", "BUDGET_EXCEEDED"):
        return {}
    return {"phase": "DISPATCHING", "status": "RUNNING",
            "tok_used": _total(state), "cost_used_cny": _total_cost(state)}


def _dispatch(state: MultiAgentState) -> list[Send] | str:
    if state.get("status") in ("CANCELED", "BUDGET_EXCEEDED") or (
            state.get("budget_blocks_by_actor")) or _total(state) >= int(state["token_cap"]):
        return "verifier"
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
    if deps.cancelled():
        deps.tracer.add("verifier", started, "任务已取消，停止调度未开始的 Worker",
                        status="blocked", stage="verifier")
        return {"phase": "CANCELED", "status": "CANCELED", "pending_repairs": []}
    if _budget_stop(state, deps):
        return {"phase": "BUDGET_EXCEEDED", "status": "BUDGET_EXCEEDED",
                "error": "Token 预算已耗尽或不足以继续取证",
                "tok_used": _total(state), "cost_used_cny": _total_cost(state),
                "pending_repairs": []}
    review, repairs = verify(state)
    current_round = int(state.get("repair_round", 0))
    exhausted = bool(repairs) and current_round >= int(state["max_repair_rounds"])
    if exhausted:
        review.verdict = ReviewVerdict.INSUFFICIENT.value
        review.issues.append("已达到最大返工轮次，按现有证据降级收敛")
        review.repair_tasks = []
        repairs = []
    verifier_bindings = (state.get("skill_bindings_by_role") or {}).get("verifier", [])
    deps.tracer.add("verifier", started,
                    f"{review.verdict}：{len(review.evidence_ids)} 份证据",
                    status="degraded" if review.verdict != "PASS" else "ok",
                    stage="verifier", output={
                        "review": review.model_dump(mode="json"),
                        "skill_bindings": verifier_bindings})
    return {
        "phase": "VERIFYING", "status": "VERIFYING",
        "reviews_by_id": {review.review_id: review.model_dump(mode="json")},
        "pending_repairs": repairs,
        "repair_round": current_round + (1 if repairs else 0),
    }


def _after_verify(state: MultiAgentState) -> list[Send] | str:
    if state.get("status") in ("CANCELED", "BUDGET_EXCEEDED"):
        return END
    repairs = state.get("pending_repairs") or []
    if _total(state) >= int(state["token_cap"]):
        return END
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
    if deps.cancelled():
        return {"phase": "CANCELED", "status": "CANCELED", "answer": "", "claims": []}
    if _budget_stop(state, deps):
        return {"phase": "BUDGET_EXCEEDED", "status": "BUDGET_EXCEEDED",
                "error": "Token 预算已耗尽，未合成答案", "answer": "", "claims": []}
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
        "join_contracts": state.get("join_contracts") or [],
    }, ensure_ascii=False, default=str)
    try:
        draft, usage = deps.structured(state, SynthesisDraft, SYNTHESIS_SYSTEM, human)
    except BudgetExceeded as exc:
        return {"phase": "BUDGET_EXCEEDED", "status": "BUDGET_EXCEEDED",
                "error": str(exc), "budget_blocks_by_actor": {"synthesizer": str(exc)},
                "answer": "", "claims": []}
    if deps.cancelled():
        deps.tracer.add("synthesizer", started, "任务在合成期间被取消，丢弃未返回答案",
                        status="blocked", stage="synthesizer", **_usage_kwargs(usage))
        return {"phase": "CANCELED", "status": "CANCELED", "answer": "", "claims": []}
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
                    output={
                        "answer": draft.answer, "claims": claims,
                        "skill_bindings": (state.get("skill_bindings_by_role") or {}).get(
                            "synthesizer", [])},
                    **_usage_kwargs(usage))
    return {
        "phase": "COMPLETED", "status": "COMPLETED",
        "answer": draft.answer, "claims": claims,
        "tok_used": _total(state) + _used_tokens(usage),
        "tok_by_actor": {"synthesizer": _used_tokens(usage)},
        "cost_used_cny": _total_cost(state) + _used_cost(usage),
        "cost_by_actor": {"synthesizer": _used_cost(usage)},
    }


def build_skeleton() -> StateGraph:
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
                                ["query_worker", "synthesizer", END])
    graph.add_edge("synthesizer", END)
    return graph


def build_graph(*, checkpointer: Any = None):
    return build_skeleton().compile(checkpointer=checkpointer)


_GRAPH = None
_GRAPH_KEY: str | None = None


def ensure_graph(cfg: Config):
    """Compile once per configured checkpoint backend.

    This reuses askdb.graph's SQLite/PostgreSQL saver factory, so single- and
    multi-agent runs have identical durability and multi-replica semantics.
    """
    from .. import auditstore, graph as graph_runtime

    global _GRAPH, _GRAPH_KEY
    key = ("pg:" + graph_runtime._pg_key()) if auditstore.enabled(cfg) \
        else str(cfg.checkpoint_db)
    if _GRAPH is None or _GRAPH_KEY != key:
        _GRAPH = graph_runtime.compile_with_checkpoint(build_skeleton(), cfg)
        _GRAPH_KEY = key
    return _GRAPH


def reset_graph() -> None:
    global _GRAPH, _GRAPH_KEY
    _GRAPH, _GRAPH_KEY = None, None
