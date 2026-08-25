"""审计流水的读取与聚合 —— 只读，不产生任何写入。

审计文件是多副本经 O_APPEND 共享追加的 JSONL（见 trace.write_audit），
这里是它唯一的消费入口：流水分页、关键词检索、时间窗统计。

两条纪律：
- **列表接口的摘要有意不含 SQL 文本与结果行。** 流水页是常开页面，
  SQL 细节只允许经 /api/replay 的字段白名单 + 配置开关出去
  （判定链路回放接口设计说明 §4.2 / §5.2）。
- 个别坏行（进程被杀时的半行）跳过而不是报错 —— 审计恰恰是出事后
  要看的页面，不能因为一次事故写坏一行就整页打不开。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 出现在流水列表里的字段。白名单式：新加字段须显式列入，
# 避免未来往审计记录里塞了敏感字段后被列表接口顺手带出去。
SUMMARY_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "org_id", "question", "rejected_by",
    "attempts", "rows_returned", "elapsed_ms", "cost_cny",
    "step_count", "multi_step",
)

# /api/replay 的字段白名单（判定链路回放接口设计说明 §4.2）。
# rows / schema_prompt 两个字段在设计上**绝不出接口** —— 用白名单而不是
# 黑名单：漏给一个无害字段是体验问题，漏挡一个敏感字段是事故。
REPLAY_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "org_id", "question",
    "tables_hit", "metrics_hit", "sql_raw", "sql_final",
    "rules_fired", "rejected_by", "attempts", "explain_rows",
    "step_count", "multi_step", "converged_early", "rows_returned",
    "elapsed_ms", "tok_in", "tok_out", "cost_cny", "steps",
)


def read_records(path: Path) -> list[dict[str, Any]]:
    """读出全部审计记录，保持文件（时间）顺序。"""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue                      # 撕裂行：跳过，不中断
            if isinstance(rec, dict) and rec.get("trace_id"):
                out.append(rec)
    return out


def _summary(rec: dict[str, Any]) -> dict[str, Any]:
    s = {k: rec.get(k) for k in SUMMARY_FIELDS}
    # 老记录没有 kind 字段：它们全部产生自 /api/ask 链路
    s["kind"] = rec.get("kind", "ask")
    s["ok"] = not rec.get("rejected_by")
    return s


def list_audits(
    path: Path, page: int = 1, page_size: int = 10,
    q: str = "", kind: str = "",
) -> dict[str, Any]:
    """流水分页，新记录在前。q 同时匹配 trace_id 与问题文本。"""
    recs = read_records(path)
    recs.reverse()
    if kind:
        recs = [r for r in recs if r.get("kind", "ask") == kind]
    if q:
        ql = q.strip().lower()
        recs = [
            r for r in recs
            if ql in str(r.get("trace_id", "")).lower()
            or ql in str(r.get("question", "")).lower()
        ]
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    start = (page - 1) * page_size
    return {
        "total": len(recs), "page": page, "page_size": page_size,
        "items": [_summary(r) for r in recs[start:start + page_size]],
    }


def get_audit(path: Path, trace_id: str) -> dict[str, Any] | None:
    """按 trace_id 取完整记录。同 id 多条时取最后一条（重放/重投递场景）。"""
    found = None
    for rec in read_records(path):
        if rec.get("trace_id") == trace_id:
            found = rec
    return found


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def stats(path: Path, days: int = 30) -> dict[str, Any]:
    """时间窗内的调用/拦截/成本统计与按日序列。

    trace_complete 按"记录里带步骤级 trace 的占比"如实计算，
    不是写死的 100% —— 页面上那格数字必须经得起对账。
    """
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    recent: list[dict[str, Any]] = []
    for rec in read_records(path):
        t = _parse_ts(str(rec.get("ts", "")))
        if t is not None and t >= cutoff:
            recent.append(rec)

    calls = len(recent)
    blocked = sum(1 for r in recent if r.get("rejected_by"))
    with_steps = sum(1 for r in recent if r.get("steps"))

    daily: dict[str, dict[str, Any]] = {}
    by_kind: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_model: dict[str, dict[str, Any]] = {}
    for r in recent:
        day = str(r.get("ts", ""))[:10]
        d = daily.setdefault(day, {"date": day, "calls": 0, "cost_cny": 0.0})
        d["calls"] += 1
        d["cost_cny"] = round(d["cost_cny"] + float(r.get("cost_cny") or 0), 6)
        by_kind[r.get("kind", "ask")] = by_kind.get(r.get("kind", "ask"), 0) + 1
        if r.get("rejected_by"):
            by_rule[str(r["rejected_by"])] = by_rule.get(str(r["rejected_by"]), 0) + 1
        # 直查不经模型（model=None）不计入模型维度；老记录无 model 字段，
        # 按调用类型如实归为"未记录"而不是猜一个模型名
        m = r.get("model") or ("（未记录）" if r.get("kind", "ask") == "ask" else None)
        if m:
            e = by_model.setdefault(m, {"calls": 0, "cost_cny": 0.0})
            e["calls"] += 1
            e["cost_cny"] = round(e["cost_cny"] + float(r.get("cost_cny") or 0), 6)

    return {
        "days": days,
        "calls": calls,
        "blocked": blocked,
        "block_rate": round(blocked / calls, 4) if calls else 0.0,
        "cost_cny": round(sum(float(r.get("cost_cny") or 0) for r in recent), 6),
        "tok_in": sum(int(r.get("tok_in") or 0) for r in recent),
        "tok_out": sum(int(r.get("tok_out") or 0) for r in recent),
        "trace_complete": round(with_steps / calls, 4) if calls else None,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "by_kind": by_kind,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost_cny"])),
    }
