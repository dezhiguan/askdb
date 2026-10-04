"""影子接入在开关关闭时不改变旧审计，打开时按白名单镜像。"""

from pathlib import Path

from askdb.audit import SUMMARY_FIELDS
from askdb.keel_shadow import keel_status, mirror_audit, with_callback
from askdb.trace import write_audit


def test_status_maps_onto_three_keel_values():
    assert keel_status("ok") == "ok"
    assert keel_status("hit") == "ok"
    assert keel_status("fallback") == "fallback"
    assert keel_status("degraded") == "fallback"
    assert keel_status("empty") == "fallback"
    assert keel_status("failed") == "failed"
    assert keel_status("blocked") == "failed"


def test_shadow_off_does_not_write_a_mirror(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEL_SHADOW", raising=False)
    spool = tmp_path / "spool.jsonl"
    monkeypatch.setenv("KEEL_AUDIT_SPOOL_PATH", str(spool))
    write_audit(tmp_path / "audit.jsonl", {"trace_id": "t1", "kind": "ask", "sql": "SELECT 1"})
    assert not spool.exists()


def test_shadow_mirror_keeps_only_summary_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEL_SHADOW", "1")
    monkeypatch.setenv("KEEL_ENV", "dev")
    spool = tmp_path / "spool.jsonl"
    monkeypatch.setenv("KEEL_AUDIT_SPOOL_PATH", str(spool))
    monkeypatch.delenv("KEEL_AUDIT_URL", raising=False)
    write_audit(tmp_path / "audit.jsonl", {
        "trace_id": "t1", "kind": "ask", "question": "订单有多少", "sql": "SELECT 1",
    })
    import json
    event = json.loads(spool.read_text(encoding="utf-8").splitlines()[0])
    assert event["action"] == "ask"
    assert event["trace_id"] == "t1"
    assert event["payload"]["question"] == "订单有多少"
    assert "sql" not in event["payload"]
    assert set(event["payload"]) <= set(SUMMARY_FIELDS)


def test_callback_is_absent_until_shadow_is_on(monkeypatch):
    monkeypatch.delenv("KEEL_SHADOW", raising=False)
    config = {"configurable": {"thread_id": "t"}}
    assert with_callback(config) is config
