"""追踪、成本归因与审计落盘。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from askdb.trace import Tracer, cost_cny, write_audit
from askdb import trace


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


# ---------------------------------------------------------------------------
# 计价三项：缓存命中、峰谷时段、按实际应答模型归价
# ---------------------------------------------------------------------------

QWEN = {"price_input_per_1k": 0.0008, "price_output_per_1k": 0.0027,
        "price_cached_input_per_1k": 0.00008}
DEEPSEEK = {"price_input_per_1k": 0.003, "price_output_per_1k": 0.009,
            "price_cached_input_per_1k": 0.0001, "offpeak_multiplier": 0.5,
            "peak_windows": ["09:00-12:00", "14:00-18:00"],
            "peak_weekdays_only": True, "peak_utc_offset_hours": 8}
BEIJING = timezone(timedelta(hours=8))


def test_cached_input_is_billed_at_the_cache_price():
    """命中的输入按缓存价，且不能重复计入未命中部分。

    实测 deepseek 直连一次调用 in=867 里 cache_read=768 —— 命中价是未命中价的
    1/30，不区分的话这一笔要贵好几倍。
    """
    peak = datetime(2026, 9, 7, 10, 0, tzinfo=BEIJING)   # 周一 10:00，高峰
    hit = trace.call_cost_cny(867, 81, 768, DEEPSEEK, at=peak)
    miss = trace.call_cost_cny(867, 81, 0, DEEPSEEK, at=peak)
    assert hit < miss
    # 99 未命中 × 0.003/1k + 768 命中 × 0.0001/1k + 81 输出 × 0.009/1k
    assert hit == round(99 / 1000 * 0.003 + 768 / 1000 * 0.0001 + 81 / 1000 * 0.009, 6)


def test_cached_cannot_exceed_input():
    """厂商回传异常时不能算出负的未命中量。"""
    assert trace.call_cost_cny(100, 0, 999, QWEN) == round(100 / 1000 * 0.00008, 6)


def test_offpeak_is_half_price_for_deepseek():
    """高峰=北京时间周一至周五 9-12、14-18，其余空闲价减半。"""
    mon_peak = datetime(2026, 9, 7, 10, 0, tzinfo=BEIJING)
    mon_gap = datetime(2026, 9, 7, 13, 0, tzinfo=BEIJING)     # 12-14 之间是空闲
    sat = datetime(2026, 9, 12, 10, 0, tzinfo=BEIJING)        # 周六整天空闲
    assert trace.peak_multiplier(DEEPSEEK, at=mon_peak) == 1.0
    assert trace.peak_multiplier(DEEPSEEK, at=mon_gap) == 0.5
    assert trace.peak_multiplier(DEEPSEEK, at=sat) == 0.5
    assert trace.call_cost_cny(1000, 1000, 0, DEEPSEEK, at=mon_gap) * 2 == \
        trace.call_cost_cny(1000, 1000, 0, DEEPSEEK, at=mon_peak)


def test_no_peak_windows_means_no_discount():
    """百炼没有峰谷 —— 不能因为没配时段就给它打个折。"""
    for t in (datetime(2026, 9, 7, 10, 0, tzinfo=BEIJING),
              datetime(2026, 9, 12, 3, 0, tzinfo=BEIJING)):
        assert trace.peak_multiplier(QWEN, at=t) == 1.0


def test_steps_with_different_models_are_billed_separately():
    """一次问答里两步由不同模型应答时，总额是两家各自单价之和。

    这正是旧口径算错的地方：它拿总 token 乘主模型单价，兜底那一步按错价记。
    """
    tr = trace.Tracer()
    t = tr.start()
    a = trace.call_cost_cny(1000, 100, 0, QWEN)
    tr.add("generate_sql", t, tok_in=1000, tok_out=100, cost_cny=a)
    b = trace.call_cost_cny(1000, 100, 0, DEEPSEEK,
                            at=datetime(2026, 9, 7, 10, 0, tzinfo=BEIJING))
    tr.add("generate_sql", t, tok_in=1000, tok_out=100, cost_cny=b)
    assert tr.cost_cny == round(a + b, 6)
    # 旧口径：总 token × 主模型单价 —— 会把兜底那步按 qwen 的价记，明显偏低
    assert tr.cost_cny > trace.cost_cny(tr.tok_in, tr.tok_out, QWEN)
