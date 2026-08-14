"""追踪、成本归因与审计落盘。"""

from __future__ import annotations

import json

from askdb.trace import Tracer, cost_cny, write_audit


def test_steps_are_recorded_in_order():
    tr = Tracer()
    tr.add("a", tr.start(), "第一步")
    tr.add("b", tr.start(), "第二步", status="blocked")
    assert [s["step"] for s in tr.as_list()] == ["a", "b"]
    assert tr.as_list()[1]["status"] == "blocked"


def test_tokens_are_summed_across_steps():
    tr = Tracer()
    tr.add("x", tr.start(), tok_in=10, tok_out=4)
    tr.add("y", tr.start(), tok_in=5, tok_out=1)
    assert tr.tok_in == 15 and tr.tok_out == 5


def test_elapsed_is_non_negative():
    assert Tracer().elapsed_ms >= 0


def test_cost_uses_configured_prices():
    llm = {"price_input_per_1k": 0.002, "price_output_per_1k": 0.01}
    assert cost_cny(1000, 1000, llm) == 0.012


def test_cost_defaults_to_zero_when_unpriced():
    assert cost_cny(1000, 1000, {}) == 0.0


def test_audit_appends_jsonl(tmp_path):
    p = tmp_path / "nested" / "audit.jsonl"
    write_audit(p, {"a": 1})
    write_audit(p, {"a": 2})
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(x)["a"] for x in lines] == [1, 2]


def test_audit_serializes_non_json_values(tmp_path):
    from datetime import datetime

    p = tmp_path / "audit.jsonl"
    write_audit(p, {"ts": datetime(2026, 8, 11)})
    assert "2026-08-11" in p.read_text(encoding="utf-8")


def test_audit_failure_does_not_raise(tmp_path):
    """写审计失败不能影响主链路 —— 查询已经完成了。"""
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    write_audit(blocked / "sub" / "audit.jsonl", {"a": 1})


def test_now_iso_has_timezone():
    from askdb.trace import now_iso
    s = now_iso()
    assert s[:2] == "20" and ("+" in s[10:] or s.endswith("Z"))





def _spam_audit(path_str: str, tag: str) -> None:
    """必须是模块级函数：spawn 启动的子进程要能 pickle 到它。"""
    from pathlib import Path

    from askdb.trace import write_audit

    for i in range(200):
        # 记录做长一些：短记录即使有缓冲也很难看出交错
        write_audit(Path(path_str), {"tag": tag, "i": i, "sql": "SELECT " + "x" * 900})


def test_audit_lines_survive_concurrent_writers(tmp_path):
    """多副本共享同一个审计文件时不得撕行 —— 审计是出事后唯一的凭据。"""
    import json
    from multiprocessing import Process

    p = tmp_path / "audit.jsonl"
    procs = [Process(target=_spam_audit, args=(str(p), t)) for t in ("a", "b", "c")]
    for x in procs:
        x.start()
    for x in procs:
        x.join()

    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 600, f"记录条数对不上：{len(lines)}"
    for ln in lines:
        json.loads(ln)          # 任何一行坏掉都会在这里炸
