"""Run one configured agent. The graph and the top-level ask share this path.

Step 1 wires the existing react loop (``agentgraph``) through the query spec.
Orchestration nodes call the same function in later steps. A caller-supplied
``llm`` wins over ``agents.<name>.model`` so tests and evals can inject a client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..llm import LlmClient
from .spec import AgentSpec, load_agent, narrow_tools


@dataclass(frozen=True)
class ReactBinding:
    spec: AgentSpec
    llm: Any
    max_steps: int
    cost_cap: int
    allowed_tools: frozenset[str]
    skill_bindings: tuple[dict[str, Any], ...]


def react_binding(cfg: Config, name: str = "query",
                  llm: Any = None, *, question: str = "",
                  pinned: list[dict[str, Any]] | None = None) -> ReactBinding:
    """Resolve model, step budget, tool ceiling and pinned Skill versions."""
    from .. import skill, tools
    from .. import agent as agent_mod

    spec = load_agent(cfg, name)
    if spec.kind != "react":
        raise ValueError(f"智能体 {name} 的 kind 是 {spec.kind}，不能走 react 循环")
    max_steps, cost_cap = agent_mod._budget(cfg)
    if spec.max_steps_explicit and spec.max_steps is not None:
        max_steps = spec.max_steps
    if spec.cost_cap_explicit and spec.cost_cap_tokens is not None:
        cost_cap = spec.cost_cap_tokens
    if llm is not None:
        client = llm
    elif spec.model_explicit:
        client = LlmClient(cfg, llm_cfg={**cfg.llm, "model": spec.model})
    else:
        client = LlmClient(cfg)

    registry = tuple(tools.REGISTRY)
    allowed = narrow_tools(spec.tools, registry)
    bindings: tuple[dict[str, Any], ...] = ()
    try:
        if pinned:
            report = skill.load_pinned(cfg, pinned)
            bindings = tuple(dict(item) for item in pinned)
        else:
            report = skill.resolve(
                cfg, role=spec.skills_role,
                source_id=cfg.source_id or "builtin",
                question=question,
                runtime_allowed_tools=registry,
                agent_allowed_tools=allowed,
            )
            bindings = tuple(item.model_dump(mode="json") for item in report.bindings)
        requested = [tool for manifest in report.manifests
                     for tool in manifest.requested_tools]
        allowed = narrow_tools(spec.tools, registry, requested)
    except Exception:
        # 解析失败时保留 spec 天花板。提示词里的口径仍由 agentgraph 里的
        # skill.render 注入；这里失败不能比那条既有路径更早把查询打死。
        if pinned:
            bindings = tuple(dict(item) for item in pinned)
    return ReactBinding(spec, client, max_steps, cost_cap, allowed, bindings)


def run_react(name: str, question: str, cfg: Config, org_id: int | None = None, *,
              executor: Any = None, llm: Any = None,
              trace_id: str | None = None, thread_id: str | None = None,
              clarification: str = "", handoff: Any = None,
              on_span: Any = None,
              parent_agent_run_id: str = "") -> Any:
    """Run one react agent to an ``AskResult``.

    Quotas, the started audit row, checkpointing and handoff match the previous
    ``agent._drive`` path. ``parent_agent_run_id`` is empty for a top-level ask
    and set when the orchestration graph calls this agent as a worker.
    """
    import uuid

    from .. import agent as agent_mod
    from .. import agentgraph, tools
    from ..executor import Executor
    from ..trace import Tracer, now_iso, write_audit

    try:
        binding = react_binding(cfg, name, llm=llm, question=question)
    except (KeyError, ValueError) as exc:
        trace_id = trace_id or uuid.uuid4().hex[:12]
        thread_id = thread_id or trace_id
        org = org_id if org_id is not None else int(
            cfg.raw.get("tenant", {}).get("default_ctx", 0) or 0)
        tracer = Tracer(on_span=on_span)
        return agent_mod._result(
            cfg, question, trace_id, thread_id, org, tracer, ok=False,
            rejected_by="CONFIG", error=str(exc),
            hint="检查 agents 配置里的 kind、工具名和步数。")

    if not binding.allowed_tools:
        trace_id = trace_id or uuid.uuid4().hex[:12]
        thread_id = thread_id or trace_id
        org = org_id if org_id is not None else int(
            cfg.raw.get("tenant", {}).get("default_ctx", 0) or 0)
        tracer = Tracer(on_span=on_span)
        return agent_mod._result(
            cfg, question, trace_id, thread_id, org, tracer, ok=False,
            rejected_by="CONFIG",
            error=f"智能体 {name} 在与 Skill 取交后没有任何可调用工具",
            hint="放宽 agents.{0}.tools，或让 Skill 申请的工具落在这份清单里。".format(name))

    trace_id = trace_id or uuid.uuid4().hex[:12]
    thread_id = thread_id or trace_id
    org = org_id if org_id is not None else int(
        cfg.raw.get("tenant", {}).get("default_ctx", 0) or 0)
    tracer = Tracer(on_span=on_span)

    dq = agent_mod.build_quota(cfg)
    over, used = dq.exhausted()
    if over:
        tracer.add("quota", tracer.start(), f"当日已用 {used}/{dq.limit}", status="blocked")
        return agent_mod._result(
            cfg, question, trace_id, thread_id, org, tracer, ok=False,
            rejected_by="QUOTA", error=f"已达当日模型调用上限（{used}/{dq.limit}）",
            hint="明日自动恢复；直查 SQL 不受配额限制。")

    try:
        write_audit(cfg, {
            "trace_id": trace_id, "ts": now_iso(), "kind": "ask",
            "phase": agent_mod.PHASE_STARTED, "thread_id": thread_id, "org_id": org,
            "question": question, "role": cfg.role or "ANONYMOUS",
            "user": cfg.user or "",
            "source": cfg.source_id or "builtin",
            "source_name": cfg.source_name or cfg.path,
            "agent": name,
        })
    except Exception:
        pass

    own_exec = executor is None
    ex = executor or Executor(cfg)
    deps = agentgraph.Deps(
        cfg=cfg, llm=binding.llm, executor=ex, tracer=tracer,
        ctx=tools.ToolContext(cfg=cfg, org_id=org, executor=ex),
        handoff=handoff,
        allowed_tools=binding.allowed_tools,
        skill_bindings=binding.skill_bindings,
        agent_name=name,
        agent_role=binding.spec.skills_role,
        agent_run_id=f"{trace_id}:{name}",
        parent_agent_run_id=parent_agent_run_id,
    )
    init = agentgraph.initial_state(
        question, org, trace_id, thread_id,
        binding.max_steps, binding.cost_cap, clarification)
    try:
        from ..keel_shadow import with_callback

        final = agentgraph.ensure_graph(cfg).invoke(
            init,
            with_callback({
                "configurable": {"thread_id": thread_id, "deps": deps},
                "recursion_limit": agentgraph.recursion_limit(binding.max_steps),
            }))
    except Exception as exc:  # noqa: BLE001
        tracer.add("finalize", tracer.start(), f"执行图异常：{exc}", status="failed")
        return agent_mod._result(
            cfg, question, trace_id, thread_id, org, tracer, ok=False,
            rejected_by="EXEC", error=f"执行链路异常：{exc}",
            hint="这不是提问本身的问题，稍后重试；持续出现请联系运维。")
    finally:
        if own_exec:
            ex.close()
    return agentgraph.to_result(final, cfg, tracer)


class _TaggedTracer:
    """把子智能体的 span 记到父追踪上，并补上归属。"""

    def __init__(self, inner: Any, *, agent_run_id: str, agent_role: str,
                 parent_agent_run_id: str) -> None:
        self._inner = inner
        self._tags = {
            "agent_run_id": agent_run_id,
            "agent_role": agent_role,
            "parent_agent_run_id": parent_agent_run_id,
        }

    def add(self, step: str, since: float, note: str = "", **kwargs: Any) -> Any:
        for key, value in self._tags.items():
            kwargs.setdefault(key, value)
        return self._inner.add(step, since, note, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


_INLINE_GRAPH = None


def _inline_graph():
    """Worker 内的 react 循环不落自己的检查点。

    父编排图已经按 thread_id 存了协议状态。再往同一个检查点写另一张图，
    续跑时两套状态会叠在同一条线程上。
    """
    global _INLINE_GRAPH
    from .. import agentgraph

    if _INLINE_GRAPH is None:
        _INLINE_GRAPH = agentgraph.build_skeleton().compile()
    return _INLINE_GRAPH


def run_react_inline(name: str, question: str, cfg: Config, org_id: int, *,
                     llm: Any, executor: Any, tracer: Any,
                     agent_run_id: str, parent_agent_run_id: str = "",
                     context: str = "", task_scope: dict[str, Any] | None = None,
                     pinned: list[dict[str, Any]] | None = None,
                     cancel_check: Any = None,
                     max_steps: int | None = None,
                     cost_cap: int | None = None) -> tuple[Any, tuple[dict[str, Any], ...]]:
    """Run one react agent inside an orchestration node.

    No started-audit row, no handoff, no second checkpoint. The caller maps
    the returned AskResult into its own protocol object.
    """
    from .. import agentgraph, tools

    binding = react_binding(cfg, name, llm=llm, question=question, pinned=pinned)
    steps = max_steps if max_steps is not None else binding.max_steps
    cap = cost_cap if cost_cap is not None else binding.cost_cap
    tagged = _TaggedTracer(
        tracer, agent_run_id=agent_run_id, agent_role=binding.spec.skills_role,
        parent_agent_run_id=parent_agent_run_id)
    deps = agentgraph.Deps(
        cfg=cfg, llm=binding.llm, executor=executor, tracer=tagged,
        ctx=tools.ToolContext(cfg=cfg, org_id=org_id, executor=executor),
        allowed_tools=binding.allowed_tools,
        skill_bindings=binding.skill_bindings,
        agent_name=name,
        agent_role=binding.spec.skills_role,
        agent_run_id=agent_run_id,
        parent_agent_run_id=parent_agent_run_id,
        cancel_check=cancel_check,
    )
    init = agentgraph.initial_state(
        question, org_id, agent_run_id, agent_run_id, steps, cap, context=context,
        task_scope=task_scope or {})
    from ..keel_shadow import with_callback

    final = _inline_graph().invoke(
        init,
        with_callback({
            "configurable": {"thread_id": agent_run_id, "deps": deps},
            "recursion_limit": agentgraph.recursion_limit(steps),
        }))
    return agentgraph.to_result(final, cfg, tagged), binding.skill_bindings
