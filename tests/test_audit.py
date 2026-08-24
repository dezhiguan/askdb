"""审计读取层：分页、检索、统计的口径必须与落盘记录逐字段对得上。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from askdb import audit


def _rec(trace_id: str, ts: str, **kw) -> dict:
    base = {
        "trace_id": trace_id, "ts": ts, "org_id": 65,
        "question": "各知识库分别有多少文档",
        "rejected_by": None, "attempts": 1, "rows_returned": 4,
        "elapsed_ms": 1200, "tok_in": 800, "tok_out": 60,
        "cost_cny": 0.001, "step_count": 1, "multi_step": False,
        "sql_raw": "SELECT 1", "sql_final": "SELECT 1 LIMIT 1000",
        "steps": [{"step": "generate", "ms": 900, "status": "ok"}],
    }
    base.update(kw)
    return base


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _now(offset_days: float = 0) -> str:
    return (datetime.now().astimezone() - timedelta(days=offset_days)).isoformat(
        timespec="seconds"
    )


def test_missing_file_is_empty(tmp_path: Path):
    out = audit.list_audits(tmp_path / "nope.jsonl")
    assert out == {"total": 0, "page": 1, "page_size": 10, "items": []}
    assert audit.stats(tmp_path / "nope.jsonl")["calls"] == 0


def test_torn_line_skipped_not_fatal(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    good = _rec("aaaaaaaaaaa1", _now())
    p.write_text(
        json.dumps(good, ensure_ascii=False) + "\n" + '{"trace_id": "半行被撕',
        encoding="utf-8",
    )
    out = audit.list_audits(p)
    assert out["total"] == 1
    assert out["items"][0]["trace_id"] == "aaaaaaaaaaa1"


def test_pagination_newest_first(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [_rec(f"{i:012x}", _now(offset_days=5 - i)) for i in range(5)])
    page1 = audit.list_audits(p, page=1, page_size=2)
    page3 = audit.list_audits(p, page=3, page_size=2)
    assert page1["total"] == 5
    # 文件尾部（最新写入）排在最前
    assert page1["items"][0]["trace_id"] == "000000000004"
    assert len(page3["items"]) == 1


def test_summary_has_no_sql_text(tmp_path: Path):
    """列表接口的红线：SQL 文本与结果行绝不出现在摘要里。"""
    p = tmp_path / "audit.jsonl"
    _write(p, [_rec("aaaaaaaaaaa1", _now())])
    item = audit.list_audits(p)["items"][0]
    assert "sql_raw" not in item and "sql_final" not in item
    assert "steps" not in item and "rows" not in item
    assert item["ok"] is True and item["kind"] == "ask"


def test_search_matches_trace_id_and_question(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now(), question="卡住的文档有哪些"),
        _rec("bbbbbbbbbbb2", _now(), question="失败率是多少"),
    ])
    assert audit.list_audits(p, q="卡住")["total"] == 1
    assert audit.list_audits(p, q="BBBBBBBBBBB2")["total"] == 1
    assert audit.list_audits(p, q="不存在")["total"] == 0


def test_kind_filter_and_default(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now()),                       # 老记录无 kind
        _rec("bbbbbbbbbbb2", _now(), kind="sql"),
    ])
    assert audit.list_audits(p, kind="ask")["total"] == 1
    assert audit.list_audits(p, kind="sql")["total"] == 1


def test_get_audit_returns_full_record_last_wins(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now(), rows_returned=1),
        _rec("aaaaaaaaaaa1", _now(), rows_returned=9),
    ])
    rec = audit.get_audit(p, "aaaaaaaaaaa1")
    assert rec is not None and rec["rows_returned"] == 9
    assert rec["sql_final"]                       # 完整记录才带 SQL
    assert audit.get_audit(p, "ffffffffffff") is None


def test_stats_window_and_block_rate(tmp_path: Path):
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec("aaaaaaaaaaa1", _now(1), cost_cny=0.002),
        _rec("bbbbbbbbbbb2", _now(2), rejected_by="R-02", cost_cny=0.0,
             kind="sql", steps=[]),
        _rec("ccccccccccc3", _now(40), cost_cny=9.9),      # 窗口外
        _rec("ddddddddddd4", "不是时间戳"),                  # 坏 ts 不计入
    ])
    st = audit.stats(p, days=30)
    assert st["calls"] == 2 and st["blocked"] == 1
    assert st["block_rate"] == 0.5
    assert st["cost_cny"] == 0.002
    assert st["by_kind"] == {"ask": 1, "sql": 1}
    assert st["by_rule"] == {"R-02": 1}
    # 带步骤 trace 的只有 1/2 —— 这格必须按实情算，不写死 100%
    assert st["trace_complete"] == 0.5
    assert len(st["daily"]) == 2
