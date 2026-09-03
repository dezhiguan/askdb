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
    "trace_id", "ts", "kind", "thread_id", "org_id", "role", "user", "question", "rejected_by",
    "attempts", "rows_returned", "elapsed_ms", "cost_cny",
    "step_count", "multi_step", "source", "source_name",
)

# /api/replay 的字段白名单（判定链路回放接口设计说明 §4.2）。
# rows / schema_prompt 两个字段在设计上**绝不出接口** —— 用白名单而不是
# 黑名单：漏给一个无害字段是体验问题，漏挡一个敏感字段是事故。
REPLAY_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "org_id", "role", "user", "question",
    "tables_hit", "metrics_hit", "sql_raw", "sql_final",
    "rules_fired", "rejected_by", "attempts", "explain_rows",
    "step_count", "multi_step", "converged_early", "rows_returned",
    "elapsed_ms", "tok_in", "tok_out", "cost_cny", "steps",
    "source", "source_name",
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
    # 角色是后加的字段，老记录没有 —— 如实标"未记录"，别默认成 ANONYMOUS
    s["role"] = rec.get("role") or "（未记录）"
    s["user"] = rec.get("user") or ""
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


def _thread_status(last: dict[str, Any]) -> str:
    """一条线程现在处于什么状态 —— 看它**最后一条**记录。

    续跑写新 trace 但 thread 不变，所以线程的当前状态永远由最后一条决定；
    归属才看第一条（见 tasks 的说明）。
    """
    if last.get("rejected_by") == "INTERRUPTED":
        return "interrupted"              # 现场还在检查点里，可续跑
    if last.get("rejected_by"):
        return "rejected"                 # 被护栏拦下，已收尾
    return "done"


def tasks(path: Path, user: str) -> list[dict[str, Any]]:
    """某个账号名下的**全部执行线程**，新的在前。

    askdb 没有任务表，任务这个概念完全落在审计流水与检查点上：
    一次提问开一条线程（thread_id），续跑写新 trace 但线程不变。
    所以"我有哪些任务" = 按 thread_id 聚合我发起过的审计记录。

    这里列全部而不是只列中断的：中断只在异常逃出执行图时才发生
    （进程故障、递归超限、检查点库异常），是故障态不是常规流程。
    只列中断等于这一页正常情况下永远是空的 —— 实际就是这么空了。
    可续跑的那些由 ``resumable`` 字段标出来，续跑入口只对它们开放。

    **必须按账号收窄。** 中断恢复设计 §4.2 原本禁止一切未完成任务的枚举，
    理由是当时没有账号体系 —— 列出来就等于任何人都能看到并续跑别人的
    任务，而任务里带着别人问过的问题原文。登录接入后前提变了：
    按发起人收窄的列表不是枚举入口。但**匿名一律不给**，那正是 §4.2
    要挡的情形，传空账号直接返回空。
    """
    if not user:
        return []

    threads: dict[str, list[dict[str, Any]]] = {}
    for rec in read_records(path):
        tid = rec.get("thread_id") or rec.get("trace_id")
        if tid:
            threads.setdefault(str(tid), []).append(rec)

    out: list[dict[str, Any]] = []
    for tid, recs in threads.items():
        # 归属看这条线程的**第一条**记录：续跑会写新 trace，但发起人不变。
        # 按最后一条判会让"谁续跑谁就成了主人"。
        if (recs[0].get("user") or "") != user:
            continue
        last = recs[-1]
        item = _summary(last)
        item["thread_id"] = tid
        item["attempts_on_thread"] = len(recs)
        item["first_ts"] = recs[0].get("ts", "")
        item["question"] = recs[0].get("question") or last.get("question") or ""
        item["status"] = _thread_status(last)
        item["resumable"] = item["status"] == "interrupted"
        out.append(item)

    out.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return out


def resumable(path: Path, user: str) -> list[dict[str, Any]]:
    """某个账号名下**尚可续跑**的任务 —— tasks() 里状态仍为中断的那些。

    /api/resume 按 thread_id 从断点继续。归属与匿名的约束同 tasks()。
    """
    return [t for t in tasks(path, user) if t["resumable"]]


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


def _percentile(values: list[int], q: float) -> int | None:
    """最近秩法取分位。样本少时它就等于某个真实观测值 —— 这是有意的：
    插值会造出一个从没发生过的耗时，而这页要的是"实际最慢的那次有多慢"。
    调用方须同时展示样本量，否则 7 次调用的 P95 会被当成稳定指标读。
    """
    if not values:
        return None
    k = max(0, min(len(values) - 1, round((len(values) - 1) * q)))
    return values[k]


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
    elapsed = sorted(int(r.get("elapsed_ms") or 0) for r in recent)

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
        "elapsed_p50_ms": _percentile(elapsed, 0.5),
        "elapsed_p95_ms": _percentile(elapsed, 0.95),
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "by_kind": by_kind,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost_cny"])),
    }
