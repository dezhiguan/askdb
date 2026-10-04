"""影子期把同一条问答再送一份给 Langfuse 和 keel-audit。

旧追踪、旧审计照常写。这里失败只记日志，不拒绝查询。
对比口径在 docs/keel-shadow.md，通过之前不删旧实现。
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("askdb.keel_shadow")

_tracing_ready = False


def enabled() -> bool:
    return os.environ.get("KEEL_SHADOW") == "1"


def keel_status(status: str) -> str:
    """OK / SOFT / 其余，对应 keel.status 的 ok、fallback、failed。"""
    from .trace import OK_STATUSES, SOFT_STATUSES

    if status in OK_STATUSES:
        return "ok"
    if status in SOFT_STATUSES:
        return "fallback"
    return "failed"


def mirror_audit(record: dict[str, Any]) -> None:
    """旧审计写成功之后再镜像一条。KEEL_SHADOW 未打开时什么都不做。"""
    if not enabled():
        return
    spool = os.environ.get("KEEL_AUDIT_SPOOL_PATH")
    if not spool:
        log.warning("KEEL_SHADOW=1 but KEEL_AUDIT_SPOOL_PATH is unset; audit mirror skipped")
        return
    from .audit import SUMMARY_FIELDS

    payload = _capture(record, SUMMARY_FIELDS)
    event = {
        "agent": "askdb",
        "env": os.environ.get("KEEL_ENV", ""),
        "action": str(record.get("kind") or "ask"),
        "risk": "low",
        "decision": "allowed",
        "trace_id": record.get("trace_id"),
        "payload": payload,
    }
    line = (json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8")
    path = Path(spool)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        while line:
            line = line[os.write(fd, line):]
    finally:
        os.close(fd)
    _post_audit(event)


def with_callback(config: dict[str, Any]) -> dict[str, Any]:
    """给图调用补上 LangGraph 回调。未开影子或装不上 SDK 时原样返回。"""
    if not enabled():
        return config
    callback = _callback()
    if callback is None:
        return config
    callbacks = list(config.get("callbacks") or [])
    callbacks.append(callback)
    return {**config, "callbacks": callbacks}


@contextmanager
def invocation(trace_id: str) -> Iterator[None]:
    """把一次问答收进同一个 OTel trace，子 span 共用这个 trace_id。"""
    if not enabled():
        yield
        return
    _ensure_tracing()
    span = None
    try:
        from opentelemetry import trace

        from keel.tracing import attrs

        tracer = trace.get_tracer("askdb.keel")
        span = tracer.start_as_current_span("askdb", attributes={
            attrs.OBSERVATION_TYPE: "agent",
            attrs.AGENT: "askdb",
            attrs.STATUS: "ok",
            "askdb.trace_id": trace_id,
        })
        span.__enter__()
    except Exception:
        span = None
    try:
        yield
    finally:
        if span is not None:
            span.__exit__(None, None, None)


@contextmanager
def agent_span(name: str, trace_id: str) -> Iterator[None]:
    """内部子智能体不单独注册，span 类型保持 agent。"""
    if not enabled():
        yield
        return
    _ensure_tracing()
    span = None
    try:
        from opentelemetry import trace

        from keel.tracing import attrs

        tracer = trace.get_tracer("askdb.keel")
        span = tracer.start_as_current_span(name, attributes={
            attrs.OBSERVATION_TYPE: "agent",
            attrs.AGENT: "askdb",
            attrs.STATUS: "ok",
            "askdb.trace_id": trace_id,
        })
        span.__enter__()
    except Exception:
        span = None
    try:
        yield
    finally:
        if span is not None:
            span.__exit__(None, None, None)


def mount(app) -> None:
    """在原有 HTTP 之外挂上 /v1/invoke。缺配置就跳过，不挡现有接口。"""
    if not enabled():
        return
    manifest = os.environ.get("KEEL_MANIFEST")
    if not manifest:
        log.warning("KEEL_SHADOW=1 but KEEL_MANIFEST is unset; /v1/invoke not mounted")
        return
    try:
        from keel.agent import Agent

        Agent.from_manifest(manifest).mount_to(app)
    except Exception:
        log.warning("keel mount skipped", exc_info=True)


def _capture(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    try:
        from keel.audit.masking import capture_fields

        return capture_fields(record, fields)
    except Exception:
        allowed = set(fields)
        return {key: value for key, value in record.items() if key in allowed}


def _post_audit(event: dict[str, Any]) -> None:
    url = os.environ.get("KEEL_AUDIT_URL")
    token = os.environ.get("KEEL_AUDIT_TOKEN")
    if not url or not token:
        return
    try:
        import httpx

        httpx.post(url.rstrip("/") + "/api/v1/audit/events", json=event,
                   headers={"Authorization": f"Bearer {token}"}, timeout=5)
    except Exception:
        log.warning("keel audit mirror post failed trace_id=%s", event.get("trace_id"))


def _callback():
    try:
        from opentelemetry import trace

        from keel.tracing.langgraph import LangGraphCallback

        return LangGraphCallback("askdb", trace.get_tracer("askdb.keel"))
    except Exception:
        return None


def _ensure_tracing() -> None:
    global _tracing_ready
    if _tracing_ready:
        return
    host = os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    buffer_path = os.environ.get("KEEL_TRACE_BUFFER_PATH")
    if not host or not public or not secret or not buffer_path:
        return
    try:
        from opentelemetry import trace

        from keel.tracing.otel import configure_tracing

        trace.set_tracer_provider(configure_tracing(host, public, secret, buffer_path))
        _tracing_ready = True
    except Exception:
        log.warning("langfuse exporter not configured", exc_info=True)
