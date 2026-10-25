"""Strict, governed Keel protocol entry; AskDB keeps its own domain safeguards."""
from __future__ import annotations

import asyncio
import hmac
import os
import re
import uuid
from pathlib import Path

from .keel_runtime import Invocation, active


def mount(app, cfg):
    from keel.agent import Agent
    from keel.protocol.errors import ErrorCode
    from starlette.responses import JSONResponse

    manifest = os.environ.get("KEEL_MANIFEST")
    token = os.environ.get("KEEL_ASKDB_TOKEN")
    env = os.environ.get("KEEL_ENV")
    if not manifest or not token or env not in ("staging", "prod"):
        raise RuntimeError("Keel requires KEEL_MANIFEST, KEEL_ASKDB_TOKEN and staging/prod KEEL_ENV")
    # SDK reads these for both prompts and tracing; set once at startup, never
    # temporarily swap process credentials while concurrent requests are running.
    for suffix in ("HOST", "PUBLIC_KEY", "SECRET_KEY"):
        value = os.environ.get("KEEL_LANGFUSE_" + suffix)
        if value:
            os.environ["LANGFUSE_" + suffix] = value
    agent = Agent.from_manifest(manifest)
    from .keel_runtime import register_tools
    register_tools(agent)
    agent.validate_config()
    from .keel_runtime import configure_native
    configure_native(agent)

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.url.path.startswith("/v1/"):
            if request.url.path == "/v1/health" and request.method == "GET":
                return await call_next(request)
            supplied = request.headers.get("X-Keel-Bridge", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"code": ErrorCode.AUTH_UNAUTHENTICATED.value,
                                     "message": "缺少有效的平台调用身份", "trace_id": "", "retryable": False}, status_code=401)
            if (request.headers.get("X-Keel-Agent", "askdb") != "askdb"
                    or request.headers.get("X-Keel-Env", env) != env):
                return JSONResponse({"code": ErrorCode.SERVER_INVALID_PARAM.value,
                                     "message": "调用智能体或环境不匹配", "trace_id": "", "retryable": False}, status_code=400)
        return await call_next(request)

    @agent.entry
    async def chat(request, ctx):
        return await invoke(request, ctx, cfg)

    agent.mount_to(app)
    app.state.keel_agent = agent


async def invoke(request, ctx, cfg):
    from keel.protocol.errors import ErrorCode, KeelError
    from .ask import run_ask
    from .keel_shadow import _query_cfg

    text = (getattr(request, "input", None) or {}).get("text", "")
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise KeelError(ErrorCode.SERVER_INVALID_PARAM)
    if ctx.eval_run_id == "self-check" and text == "probe":
        ctx.step("protocol.ready")
        return ctx.final("askdb ready")
    # Evaluation uses synthetic data, a separate config and fresh checkpoint IDs.
    # It never consults the production source registry or user role database.
    if ctx.eval_run_id:
        from .config import load
        query_cfg = load(Path(__file__).resolve().parents[1] / "config/keel-eval.yaml")
    else:
        query_cfg, why = await asyncio.to_thread(_query_cfg, cfg)
        if query_cfg is None:
            raise KeelError(ErrorCode.SERVER_INVALID_PARAM, why)
        from . import auth, identity
        user = ctx.user_id or ""
        if not user:
            raise KeelError(ErrorCode.AUTH_UNAUTHENTICATED, "问数需要已登录的平台用户")
        roles = await asyncio.to_thread(auth.roles_of, cfg, user)
        if not roles or not identity.can(roles, identity.QUERY):
            raise KeelError(ErrorCode.TOOL_NOT_GRANTED, "该平台账号尚未获得问数查询权限")
        query_cfg = identity.for_roles(query_cfg, roles, user=user)

    source = query_cfg.source_id or "builtin"
    await ctx.audit.record("ask", "low", "allowed", payload={"kind": "ask", "source": source},
                           trace_id=ctx.trace_id, run_id=ctx.run_id)
    invocation = Invocation(ctx, asyncio.get_running_loop())
    binding = active.set(invocation)

    def progress(span):
        name = getattr(span, "name", None)
        if name is None and isinstance(span, dict):
            name = span.get("name") or span.get("step")
        name = str(name or "query")
        name = re.sub(r"[^a-zA-Z0-9_.:-]", "_", name)[:100]
        invocation.loop.call_soon_threadsafe(ctx.step, name)

    try:
        result = await asyncio.to_thread(
            run_ask, text, query_cfg, requested_mode="auto", source_configs={source: query_cfg},
            org_id=None, trace_id=ctx.trace_id, thread_id=uuid.uuid4().hex,
            user=query_cfg.user, on_span=progress)
        await ctx.audit.record("ask.result", "low", "allowed" if result.ok else "denied",
                               payload={"kind": "ask.result", "source": source,
                                        "rows_returned": result.row_count,
                                        "rejected_by": result.rejected_by,
                                        "elapsed_ms": result.elapsed_ms},
                               trace_id=ctx.trace_id, run_id=ctx.run_id)
        if not result.ok and isinstance(invocation.failure, KeelError):
            raise invocation.failure
        if result.rejected_by in ("LLM", "QUOTA", "EXECUTOR", "INTERNAL"):
            raise KeelError(ErrorCode.SERVER_INTERNAL_ERROR, "问数执行失败，请查看本次链路")
        return ctx.final(reply(result), meta={"askdb": {
            "ok": result.ok, "row_count": result.row_count,
            "rejected_by": result.rejected_by, "execution_mode": result.execution_mode,
            "source": source, "thread_id": result.thread_id,
        }})
    finally:
        invocation.stopped = True
        active.reset(binding)


def reply(result):
    from .graph import jsonable
    parts = [value for value in (result.error, result.reasoning) if value]
    if result.sql_final:
        parts.append("```sql\n" + result.sql_final + "\n```")
    if result.columns:
        def cell(value):
            return str(jsonable(value)).replace("|", "\\|").replace("\n", " ")
        parts.append("| " + " | ".join(map(cell, result.columns)) + " |\n| "
                     + " | ".join("---" for _ in result.columns) + " |\n"
                     + "\n".join("| " + " | ".join(map(cell, row)) + " |" for row in result.rows))
    parts.extend(value for value in (result.caliber, result.empty_note, result.scope_note) if value)
    return "\n\n".join(parts) or "没有结果"
