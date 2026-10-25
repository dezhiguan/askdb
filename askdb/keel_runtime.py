"""Invocation-scoped bridge from AskDB's synchronous graph to Keel's async SDK.

No global model clients or event loops: parallel graph workers inherit the bridge
through LangGraph's copied context, and every SDK operation runs on its owner loop.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

tool_context: ContextVar[Any] = ContextVar("askdb_keel_tool_context")

active: ContextVar["Invocation | None"] = ContextVar("askdb_keel_invocation", default=None)


@dataclass
class Invocation:
    ctx: Any
    loop: asyncio.AbstractEventLoop
    stopped: bool = False
    model_calls: int = 0
    failure: Exception | None = None
    window: Any = None

    def call(self, coroutine):
        if self.stopped:
            coroutine.close()
            raise concurrent.futures.CancelledError("AskDB invocation stopped")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        while True:
            try:
                return future.result(timeout=0.25)
            except concurrent.futures.TimeoutError:
                if future.done():
                    raise
                if self.stopped:
                    future.cancel()
                    raise concurrent.futures.CancelledError("AskDB invocation stopped")

    async def tool(self, name, args, domain):
        from .tools import ToolResult
        binding = tool_context.set(domain)
        try:
            result = await self.ctx.tools.call(name, **args)
            return ToolResult(**result)
        finally:
            tool_context.reset(binding)

    async def structured(self, schema, system: str, human: str, *, timeout: float = 20, temperature: float = 0):
        from .keel_prompts import resolve
        from keel.llm.client import ChatResult
        from .llm import LlmUsage

        name, variables = resolve(system)
        prompt = await self.ctx.prompt(name)
        # Keep AskDB's configured sampling when the managed prompt has no
        # override. Copy the reference so another invocation's cached prompt
        # keeps its original versioned configuration.
        from copy import copy
        prompt = copy(prompt)
        prompt.config = {"temperature": temperature, **prompt.config}
        messages = [{"role": "system", "content": prompt.compile(**variables)},
                    {"role": "user", "content": human}]
        tool = {"type": "function", "function": {
            "name": schema.__name__, "description": schema.__doc__ or schema.__name__,
            "parameters": schema.model_json_schema(),
        }}
        prior = getattr(self.ctx, "_session_messages", None) or []
        if prior and not self.ctx.eval_run_id:
            if self.window is None:
                from keel.context_window import ContextWindow
                self.window = ContextWindow(
                    self.ctx.llm, self.ctx.memory, self.ctx.agent, self.ctx.env,
                    self.ctx.session_id, self.ctx.user_id, True, self.ctx.eval_run_id, self.ctx.trace_id)
            messages = await self.window.assemble(messages[:1], prior, messages[1:], [tool])
        from keel.protocol.errors import ErrorCode, KeelError
        from pydantic import ValidationError
        manifest = getattr(self.ctx, "manifest", None)
        for attempt in range(2):
            if manifest is not None and manifest.spec.models is not None:
                configured = manifest.spec.models.budget.maxSteps or 8
                limit = min(configured, int(self.ctx.budget.get("steps", configured)))
                if self.model_calls >= limit:
                    raise KeelError(ErrorCode.AGENT_MAX_STEPS_EXCEEDED)
            self.model_calls += 1
            try:
                result = await asyncio.wait_for(self.ctx.llm.chat(
                    messages, tools=[tool], tool_choice={"type": "function", "function": {"name": schema.__name__}},
                    prompt=prompt, emit_tokens=False,
                    max_tokens=min(int(prompt.config.get("max_tokens", 4096)), 4096)), timeout=timeout)
                if not isinstance(result, ChatResult) or len(result.tool_calls) != 1:
                    raise ValueError("模型未返回唯一的结构化结果")
                call = result.tool_calls[0]
                if call.name != schema.__name__:
                    raise ValueError("模型返回了未声明的结构化工具")
                return schema.model_validate(call.arguments), LlmUsage(
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens)
            except KeelError as exc:
                if attempt or not isinstance(exc.__cause__, (json.JSONDecodeError, TypeError)):
                    raise
            except (ValueError, ValidationError):
                if attempt:
                    raise



# Configured once at service startup. Native HTTP/CLI query entrypoints keep the
# already narrowed AskDB config while using the same governed SDK capabilities.
_native_agent = None
_native_tracer = None


def configure_native(agent):
    global _native_agent, _native_tracer
    from keel.tracing.otel import configure_from_env
    from opentelemetry import trace
    _native_agent = agent
    provider = configure_from_env()
    _native_tracer = provider.get_tracer("askdb.native") if provider else trace.get_tracer("askdb.native")


def run_native(subject, cfg, *, operation=None, **kwargs):
    if _native_agent is None:
        raise RuntimeError("Keel runtime has not been initialized")

    async def execute():
        from keel.context import Context
        from .ask import run_ask
        import uuid
        legacy_trace_id = kwargs.get("trace_id") or uuid.uuid4().hex
        run_id = kwargs.get("thread_id") or uuid.uuid4().hex
        with _native_tracer.start_as_current_span("askdb.native", attributes={
                "langfuse.observation.type": "agent", "keel.agent": "askdb",
                "keel.env": os.environ["KEEL_ENV"], "askdb.trace_id": legacy_trace_id}) as span:
            identity = span.get_span_context()
            trace_id = f"{identity.trace_id:032x}" if identity.is_valid else legacy_trace_id
            ctx = Context("askdb", run_id, trace_id, asyncio.Queue(),
                          _native_tracer, manifest=_native_agent.manifest,
                          env=os.environ["KEEL_ENV"], user_id=cfg.user,
                          tool_functions=_native_agent._tools)
            invocation = Invocation(ctx, asyncio.get_running_loop())
            binding = active.set(invocation)
            try:
                return await asyncio.to_thread(operation or run_ask, subject, cfg, **kwargs)
            finally:
                invocation.stopped = True
                active.reset(binding)
                await ctx.aclose()

    return asyncio.run(execute())


def register_tools(agent):
    from .tools import REGISTRY, Tier

    def create_callback(name):
        async def callback(**kwargs):
            domain = tool_context.get()
            result = await asyncio.to_thread(REGISTRY[name].call, kwargs, domain)
            return result.to_dict()
        return callback

    for name, tool in REGISTRY.items():
        if tool.tier is not Tier.SIDE_EFFECT:
            agent.tool(name=name)(create_callback(name))


def mirror_native_audit(record):
    """Native read-only SQL/cache paths also use the canonical SDK audit event."""
    if _native_agent is None:
        raise RuntimeError("Keel runtime has not been initialized")

    async def record_event():
        from keel.audit.reporter import AuditReporter
        audit = AuditReporter("askdb", os.environ["KEEL_ENV"],
                              _native_agent.manifest.spec.audit.captureFields or ())
        try:
            return await audit.record("legacy." + str(record.get("kind") or "ask"),
                                      "low", "allowed", payload=record,
                                      trace_id=record.get("trace_id"))
        finally:
            await audit.aclose()

    return asyncio.run(record_event())
