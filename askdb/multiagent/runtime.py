"""Public runtime entrypoint and AskResult compatibility adapter."""

from __future__ import annotations

import uuid
import threading
from typing import Any

from ..config import Config
from ..graph import AskResult, _audit_of
from ..llm import LlmClient
from ..qcache import scope as scope_fingerprint
from ..quota import build_quota
from ..trace import Tracer, now_iso, write_audit
from .evidence_store import latest_by_subtask
from .federation import approved_contracts, parse_contracts
from .state import initial_state
from .supervisor_graph import MultiAgentDeps, ensure_graph


_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, threading.Event] = {}


def _cancel_event(thread_id: str) -> threading.Event:
    with _CANCEL_LOCK:
        return _CANCEL_EVENTS.setdefault(thread_id, threading.Event())


def cancel(thread_id: str, cfg: Config) -> bool:
    """Cooperatively stop unstarted workers and persist the canceled terminal state."""
    try:
        graph = ensure_graph(cfg)
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = graph.get_state(config)
        values = snapshot.values or {}
        if not values.get("plan") or values.get("status") in ("COMPLETED", "CANCELED"):
            return False
        graph.update_state(config, {"phase": "CANCELED", "status": "CANCELED"})
        _cancel_event(thread_id).set()
        return True
    except Exception:
        return False


def settings(cfg: Config) -> dict[str, Any]:
    raw = cfg.raw.get("multi_agent") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "mode": str(raw.get("mode", "off")).lower(),
        "trigger": str(raw.get("trigger", "auto")).lower(),
        "max_workers": max(1, int(raw.get("max_workers", 3))),
        "max_parallel": max(1, int(raw.get("max_parallel", 3))),
        "max_repair_rounds": max(0, int(raw.get("max_repair_rounds", 2))),
        "cost_cap_tokens": max(1, int(raw.get("cost_cap_tokens", 30_000))),
        "allow_cross_source": bool(raw.get("allow_cross_source", False)),
        "join_contracts": list(raw.get("join_contracts") or []),
    }


def run_multi_agent(
    question: str,
    cfg: Config,
    org_id: int | None = None,
    *,
    source_configs: dict[str, Config] | None = None,
    trace_id: str | None = None,
    thread_id: str | None = None,
    on_span: Any = None,
    shadow_of: str = "",
) -> AskResult:
    opts = settings(cfg)
    trace_id = trace_id or uuid.uuid4().hex[:12]
    thread_id = thread_id or trace_id
    org = org_id if org_id is not None else int(
        cfg.raw.get("tenant", {}).get("default_ctx", 0) or 0)
    tracer = Tracer(on_span=on_span)
    source_id = cfg.source_id or "builtin"
    sources = source_configs or {source_id: cfg}

    quota = build_quota(cfg)
    over, used = quota.exhausted()
    if over:
        return _failed(question, trace_id, thread_id, org, tracer, "QUOTA",
                       f"已达当日模型调用上限（{used}/{quota.limit}）")
    if len(sources) > 1 and not opts["allow_cross_source"]:
        return _failed(question, trace_id, thread_id, org, tracer, "POLICY",
                       "当前实例未开启跨数据源编排")
    try:
        joins = approved_contracts(set(sources), parse_contracts(opts["join_contracts"]))
    except ValueError as exc:
        return _failed(question, trace_id, thread_id, org, tracer, "P12", str(exc))
    scope_fingerprints = {source: scope_fingerprint(source_cfg)
                          for source, source_cfg in sources.items()}

    try:
        write_audit(cfg, {
            "trace_id": trace_id, "ts": now_iso(), "kind": "ask",
            "phase": "started", "execution_mode": "multi",
            "shadow": bool(shadow_of), "shadow_of": shadow_of,
            "thread_id": thread_id, "org_id": org, "question": question,
            "role": cfg.role or "ANONYMOUS", "user": cfg.user or "",
            "source": source_id, "sources": sorted(sources),
            "source_name": cfg.source_name or cfg.path,
        })
    except Exception:
        pass

    deps = MultiAgentDeps(
        cfg=cfg, llm=LlmClient(cfg), tracer=tracer, source_configs=sources,
        cancel_event=_cancel_event(thread_id))
    state = initial_state(
        question=question, run_id=trace_id, thread_id=thread_id,
        org_id=org, source_id=source_id, requested_mode="multi",
        max_workers=opts["max_workers"],
        max_repair_rounds=opts["max_repair_rounds"],
        token_cap=opts["cost_cap_tokens"],
        scope_fingerprints=scope_fingerprints,
        join_contracts=[item.model_dump(mode="json") for item in joins],
    )
    try:
        final = ensure_graph(cfg).invoke(
            state,
            {"configurable": {"thread_id": thread_id, "deps": deps},
             "recursion_limit": max(25, opts["max_repair_rounds"] * 8 + 20),
             "max_concurrency": opts["max_parallel"]},
        )
        result = to_result(final, cfg, tracer)
    except Exception as exc:  # graph/checkpoint errors become structured failures
        tracer.add("multi_agent", tracer.start(), f"编排失败：{exc}", status="failed")
        result = _failed(question, trace_id, thread_id, org, tracer, "EXEC",
                         f"多智能体编排失败：{exc}")
    try:
        record = _audit_of(result, cfg, "multi_shadow" if shadow_of else "ask")
        record.update({"shadow": bool(shadow_of), "shadow_of": shadow_of})
        write_audit(cfg, record)
    except Exception:
        pass
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(thread_id, None)
    return result


def resume_multi_agent(
    thread_id: str,
    cfg: Config,
    *,
    clarification: str = "",
    question: str = "",
    org_id: int | None = None,
    on_span: Any = None,
) -> AskResult | None:
    """Resume the persisted graph path, or re-plan in the same thread with new input.

    A cold process reconstructs only Runtime dependencies; task/subtask/evidence and
    pinned Skill bindings come from the checkpoint. Current source permissions are
    checked again before any node is allowed to continue.
    """
    graph = ensure_graph(cfg)
    configurable = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(configurable)
    values = dict(snapshot.values or {})
    if not values:
        return None
    if values.get("status") == "CANCELED":
        tracer = Tracer(on_span=on_span)
        return _failed(str(values.get("question", "")),
                       str(values.get("run_id", thread_id)), thread_id,
                       int(values.get("org_id", 0)), tracer, "CANCELED",
                       "任务已取消，不能继续执行")
    expected_source = str(values.get("source_id") or "builtin")
    current_source = cfg.source_id or "builtin"
    if expected_source != current_source:
        tracer = Tracer(on_span=on_span)
        return _failed(
            str(values.get("question", "")), str(values.get("run_id", thread_id)),
            thread_id, int(values.get("org_id", 0)), tracer, "RESUME_BLOCKED",
            f"任务原数据源为 {expected_source}，当前授权数据源为 {current_source}",
        )
    expected_scope = (values.get("scope_fingerprints") or {}).get(expected_source, "")
    current_scope = scope_fingerprint(cfg)
    if expected_scope and expected_scope != current_scope:
        tracer = Tracer(on_span=on_span)
        return _failed(
            str(values.get("question", "")), str(values.get("run_id", thread_id)),
            thread_id, int(values.get("org_id", 0)), tracer, "RESUME_BLOCKED",
            "当前权限、表白名单、脱敏或 Guard 配置已变化，旧 Checkpoint 不再获授权",
        )
    rewritten = (question or "").strip()
    extra = (clarification or "").strip()
    if rewritten or extra:
        base_question = rewritten or str(values.get("question", ""))
        combined = base_question + (f"\n补充条件：{extra}" if extra else "")
        return run_multi_agent(
            combined, cfg, org_id=org_id, thread_id=thread_id, on_span=on_span)

    opts = settings(cfg)
    tracer = Tracer(on_span=on_span)
    deps = MultiAgentDeps(cfg=cfg, llm=LlmClient(cfg), tracer=tracer,
                          source_configs={current_source: cfg},
                          cancel_event=_cancel_event(thread_id))
    run_id = str(values.get("run_id") or thread_id)
    try:
        final = graph.invoke(
            None,
            {"configurable": {"thread_id": thread_id, "deps": deps},
             "recursion_limit": max(25, opts["max_repair_rounds"] * 8 + 20),
             "max_concurrency": opts["max_parallel"]},
        )
        result = to_result(final, cfg, tracer)
    except Exception as exc:
        result = _failed(str(values.get("question", "")), run_id, thread_id,
                         int(values.get("org_id", 0)), tracer, "EXEC",
                         f"多智能体恢复失败：{exc}")
    try:
        write_audit(cfg, _audit_of(result, cfg, "resume"))
    except Exception:
        pass
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(thread_id, None)
    return result


def is_resumable(thread_id: str, cfg: Config) -> bool | None:
    try:
        snapshot = ensure_graph(cfg).get_state(
            {"configurable": {"thread_id": thread_id}})
    except Exception:
        return None
    values = snapshot.values or {}
    if not values.get("plan"):
        return None
    if values.get("status") == "CANCELED":
        return False
    return bool(snapshot.next)


def progress(thread_id: str, cfg: Config) -> dict[str, Any] | None:
    try:
        snapshot = ensure_graph(cfg).get_state(
            {"configurable": {"thread_id": thread_id}})
    except Exception:
        return None
    values = snapshot.values or {}
    if not values.get("plan"):
        return None
    tasks = list((values.get("subtasks_by_id") or {}).values())
    done = sum(task.get("status") == "SUCCEEDED" for task in tasks)
    return {
        "phase": str(values.get("phase") or "CREATED"),
        "step": done,
        "max_steps": len(tasks),
        "tool": "multi_agent",
        "agents": [{"id": task.get("subtask_id"), "title": task.get("title"),
                    "status": task.get("status"), "attempt": task.get("attempt", 0)}
                   for task in tasks],
        "next": list(snapshot.next or ()),
    }


def to_result(state: dict[str, Any], cfg: Config, tracer: Tracer) -> AskResult:
    active = latest_by_subtask(state.get("evidence_by_id") or {})
    evidence = list(active.values())
    primary = evidence[0] if evidence else {}
    plan = dict(state.get("plan") or {})
    if plan:
        plan["subtasks"] = list((state.get("subtasks_by_id") or {}).values())
    reviews = list((state.get("reviews_by_id") or {}).values())
    bindings_by_role = state.get("skill_bindings_by_role") or {}
    unique_bindings: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rows in bindings_by_role.values():
        for row in rows:
            key = (str(row.get("skill_id", "")), str(row.get("version", "")),
                   str(row.get("checksum", "")))
            unique_bindings[key] = row
    status = state.get("status")
    ok = status == "COMPLETED"
    contract = state.get("semantic_contract") or {}
    result = AskResult(
        ok=ok,
        question=state["question"],
        trace_id=state["run_id"],
        thread_id=state["thread_id"],
        org_id=int(state.get("org_id", 0)),
        reasoning=str(state.get("answer", "")),
        sql_final=str(primary.get("sql_final", "")),
        columns=list(primary.get("columns", [])),
        rows=list(primary.get("rows", [])),
        row_count=int(primary.get("row_count", 0)),
        truncated=any(bool(item.get("truncated")) for item in evidence),
        as_of=str(primary.get("as_of", "")),
        explain_rows=primary.get("explain_rows"),
        rules_fired=list(primary.get("rules_fired", [])),
        rewrites=list(primary.get("rewrites", [])),
        masked_columns=sorted({column for item in evidence
                               for column in item.get("masked_columns", [])}),
        mask_degraded=any(bool(item.get("mask_degraded")) for item in evidence),
        rejected_by=None if ok else ("CANCELED" if status == "CANCELED" else "EXEC"),
        error=str(state.get("error", "")),
        tables_hit=[],
        caliber="；".join(str(contract.get(key, "")) for key in
                           ("metric_definition", "time_window", "grain")
                           if contract.get(key)),
        attempts=max([int(task.get("attempt", 0)) for task in
                      (state.get("subtasks_by_id") or {}).values()] or [1]),
        step_count=len(state.get("subtasks_by_id") or {}),
        multi_step=True,
        sub_steps=list((state.get("subtasks_by_id") or {}).values()),
        steps=tracer.as_list(),
        elapsed_ms=tracer.elapsed_ms,
        tok_in=tracer.tok_in,
        tok_out=tracer.tok_out,
        cost_cny=tracer.cost_cny,
        execution_mode="multi",
        plan=plan,
        evidence=evidence,
        reviews=reviews,
        claims=list(state.get("claims") or []),
        skill_bindings=list(unique_bindings.values()),
    )
    return result


def _failed(question: str, trace_id: str, thread_id: str, org_id: int,
            tracer: Tracer, rejected_by: str, error: str) -> AskResult:
    return AskResult(
        ok=False, question=question, trace_id=trace_id, thread_id=thread_id,
        org_id=org_id, rejected_by=rejected_by, error=error,
        execution_mode="multi", steps=tracer.as_list(), elapsed_ms=tracer.elapsed_ms,
        tok_in=tracer.tok_in, tok_out=tracer.tok_out, cost_cny=tracer.cost_cny,
    )
