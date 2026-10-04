"""One entry for a question, a resume, a progress read, and a cancel.

Simple questions run the query agent. Complex questions enter the
orchestration graph. The checkpoint that already exists decides which graph
a resume continues; in-flight checkpoints are not rewritten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .config import Config
from .multiagent.router import decide_route
from .multiagent.runtime import settings


@dataclass(frozen=True)
class AskRoute:
    use_multi: bool
    shadow: bool
    enabled: bool
    orch_mode: str
    allow_cross_source: bool


def resolve_route(question: str, cfg: Config, *, requested_mode: str,
                  source_count: int) -> AskRoute:
    """Router plus the orchestration switches. No model call."""
    opts = settings(cfg)
    route = decide_route(
        question, requested_mode=requested_mode, source_count=source_count)
    enabled = bool(opts["enabled"])
    orch_mode = str(opts["mode"])
    use_multi = bool(
        enabled and orch_mode != "off"
        and (requested_mode == "multi"
             or (requested_mode == "auto" and orch_mode in ("assist", "enforce")
                 and route.route == "multi")))
    shadow = bool(
        enabled and orch_mode == "shadow"
        and requested_mode == "auto" and route.route == "multi")
    return AskRoute(
        use_multi=use_multi,
        shadow=shadow,
        enabled=enabled,
        orch_mode=orch_mode,
        allow_cross_source=bool(opts["allow_cross_source"]),
    )


def run_ask(question: str, cfg: Config, *, requested_mode: str,
            source_configs: dict[str, Config] | None,
            org_id: int | None, trace_id: str, thread_id: str,
            handoff: Any = None, on_span: Any = None,
            user: str = "", per_user: int | None = None,
            route: AskRoute | None = None,
            source_catalog: dict[str, list[str]] | None = None,
            source_names: dict[str, str] | None = None) -> Any:
    """Run the routed question. Shadow keeps the query-agent answer in front."""
    from . import agent as agent_mod
    from . import async_runner
    from .multiagent import runtime as multi_runtime

    from .keel_shadow import agent_span, invocation

    with invocation(trace_id):
        with agent_span("router", trace_id):
            chosen = route or resolve_route(
                question, cfg, requested_mode=requested_mode,
                source_count=len(source_configs or {cfg.source_id or "builtin": cfg}))
        if chosen.use_multi:
            return multi_runtime.run_multi_agent(
                question, cfg, org_id=org_id, source_configs=source_configs,
                source_catalog=source_catalog, source_names=source_names,
                trace_id=trace_id, thread_id=thread_id, on_span=on_span)
        primary = agent_mod.run_agent(
            question, cfg, org_id=org_id, trace_id=trace_id, thread_id=thread_id,
            handoff=handoff, on_span=on_span)
        if chosen.shadow:
            shadow_id = uuid.uuid4().hex[:12]
            async_runner.submit_background(
                lambda: multi_runtime.run_multi_agent(
                    question, cfg, org_id=org_id, source_configs=source_configs,
                    source_catalog=source_catalog, source_names=source_names,
                    trace_id=shadow_id, thread_id=shadow_id, shadow_of=trace_id,
                    shadow_baseline=primary),
                user=user, per_user=per_user if per_user is not None else 1)
        return primary


def _orchestration_checkpoint(thread_id: str, cfg: Config) -> bool:
    """True when this thread's checkpoint belongs to the orchestration graph."""
    from .multiagent.supervisor_graph import ensure_graph

    try:
        snapshot = ensure_graph(cfg).get_state(
            {"configurable": {"thread_id": thread_id}})
    except Exception:
        return False
    return bool((snapshot.values or {}).get("plan"))


def resume_ask(thread_id: str, cfg: Config, *, clarification: str = "",
               question: str = "", org_id: int | None = None,
               handoff: Any = None) -> Any:
    """Continue the graph that wrote the checkpoint. Do not migrate it."""
    from . import agentgraph
    from .multiagent import runtime as multi_runtime

    if _orchestration_checkpoint(thread_id, cfg):
        return multi_runtime.resume_multi_agent(
            thread_id, cfg, clarification=clarification, question=question,
            org_id=org_id)
    return agentgraph.resume(
        thread_id, cfg, clarification=clarification, question=question,
        org_id=org_id, handoff=handoff)


def progress_ask(thread_id: str, cfg: Config) -> dict[str, Any] | None:
    """Read whichever checkpoint this thread actually has."""
    from . import agentgraph
    from .multiagent import runtime as multi_runtime

    multi = multi_runtime.progress(thread_id, cfg)
    if multi is not None:
        return multi
    return agentgraph.progress(thread_id, cfg)


def should_publish(result: Any, thread_id: str, cfg: Config) -> bool:
    """A canceled orchestration run must not publish a later success."""
    if getattr(result, "execution_mode", "single") != "multi":
        return True
    from .multiagent.runtime import is_canceled

    return not is_canceled(thread_id, cfg)


def cancel_ask(thread_id: str, cfg: Config) -> bool:
    """Cooperatively stop an orchestration run."""
    from .multiagent.runtime import cancel

    return cancel(thread_id, cfg)
