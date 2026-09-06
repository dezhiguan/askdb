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

import hashlib
import json
from collections import Counter
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

# /api/trace 的字段白名单：执行追踪页要的是**节点链与计量**。
# 与 REPLAY_FIELDS 的分界是刻意的 —— 这里不给 sql_raw / sql_final / question /
# tables_hit，SQL 文本、问题原文与命中表仍然只经 /api/replay 出去（要登录、
# 要开关、还要按调用者当下的可见表收窄）。步骤 note 里会出现表名，所以
# /api/trace 同样做那道可见表收窄，只是不返回 tables_hit 本身。
TRACE_FIELDS = (
    "trace_id", "ts", "kind", "thread_id", "role", "model",
    "tok_in", "tok_out", "step_count", "multi_step", "attempts",
    "elapsed_ms", "cost_cny", "rejected_by", "source", "source_name",
)

# 步骤对象自身也走白名单 —— 记录里的 steps 由各节点自由追加，
# 哪天有人往里塞了 sql 或行样本，这里不会顺手带出去。
STEP_FIELDS = ("step", "status", "ms", "tok_in", "tok_out", "note")

# 真正过模型的图节点。与前端 traceSteps.ts 的 STEP_TYPE == 'MODEL' 是同一份口径，
# 两边都写一次是因为一个算数、一个只做展示；漂了会让「模型调用成功率」这格
# 与页面上标 MODEL 的那些 span 对不上 —— tests 里钉住了两边一致。
MODEL_STEPS = frozenset({"plan", "generate_sql", "assess", "reflect"})


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
    q: str = "", kind: str = "", with_text: bool = True,
    only_user: str | None = None,
) -> dict[str, Any]:
    """流水分页，新记录在前。q 同时匹配 trace_id 与问题文本。

    with_text=False 时**问题原文不出接口**，且 q 只匹配 trace_id。

    两件事必须一起做：只把 question 抹掉、仍允许按文本搜，等于留了一个预言机
    —— 搜"广州"能搜出 12 条，就已经把内容说出来了。遮蔽和检索面是同一道边界，
    分开做等于没做。

    only_user 不为 None 时只返回该用户发起的记录（产品与测试角色就是这样看
    审计的：只看自己的）。**这一层过滤排在 q 与分页之前**，理由和上面那条
    完全一样 —— 先搜后滤会让 total 泄露别人有多少条命中，那也是一个预言机。
    空串是合法取值：它表示"只看没有发起人的记录"，不是"不过滤"。
    """
    recs = read_records(path)
    recs.reverse()
    if only_user is not None:
        recs = [r for r in recs if (r.get("user") or "") == only_user]
    if kind:
        recs = [r for r in recs if r.get("kind", "ask") == kind]
    if q:
        ql = q.strip().lower()
        recs = [
            r for r in recs
            if ql in str(r.get("trace_id", "")).lower()
            or (with_text and ql in str(r.get("question", "")).lower())
        ]
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    start = (page - 1) * page_size
    return {
        "total": len(recs), "page": page, "page_size": page_size,
        "items": [_redact(_summary(r), with_text) for r in recs[start:start + page_size]],
        # 页面据此显示遮蔽提示，而不是让人以为这些记录本来就没有问题文本
        "text_visible": with_text,
    }


def _redact(item: dict[str, Any], with_text: bool) -> dict[str, Any]:
    """未登录时抹掉能指认到人的那两个字段。

    留下的是时间、角色、护栏结果、耗时与成本 —— 那些是**聚合与结构**，
    也正是这一页要展示的东西（护栏在拦什么、拦了多少、贵不贵）。
    抹掉的是问题原文与发起人：它们是别人问过的内容，不是这一页的展示目标。

    抹成 None 而不是删键：前端按字段渲染，少一个键会变成 undefined 到处冒，
    而 None 是一个明确的"这里有东西但你看不到"。
    """
    if with_text:
        return item
    return {**item, "question": None, "user": ""}


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

    **按发起人收窄**，登录与匿名同一条规则：user 就是"谁"，空串是匿名这一档。
    所以匿名看到的是匿名发起的线程，看不到任何登录用户的 —— 收窄本身没有
    被放松，放松的只是"匿名有没有资格看自己那一档"。

    这与 /api/resume 的归属校验是同一条口径（有主的线程只有主人能续跑，
    无主的凭 thread_id 续跑）。两处必须一致，否则会出现"列得出来、续不了"。
    """
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


def trace_chain(rec: dict[str, Any]) -> dict[str, Any]:
    """一条记录的节点链视图（/api/trace 的响应体）。

    执行追踪页此前把这些字段挂在 /api/replay 上，而回放要登录、要开关、
    连真实库的实例默认关着 —— 于是那一页最常见的样子是右半屏全是占位符，
    而节点链本身在审计记录里一直都有，不含 SQL 文本也不含结果行。

    sql_hash 是就地算的：记录里只存 SQL 全文，而追踪页那一格要的是哈希。
    哈希不可逆，给出去不等于给 SQL；但它足以判断"两次查询是不是同一条 SQL"。
    """
    out: dict[str, Any] = {k: rec.get(k) for k in TRACE_FIELDS}
    out["kind"] = rec.get("kind", "ask")
    out["steps"] = [
        {k: s.get(k) for k in STEP_FIELDS if s.get(k) is not None}
        for s in (rec.get("steps") or [])
    ]
    sql = str(rec.get("sql_final") or rec.get("sql_raw") or "")
    out["sql_hash"] = hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None
    return out


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


def stats(path: Path, days: int = 30, only_user: str | None = None) -> dict[str, Any]:
    """时间窗内的调用/拦截/成本统计与按日序列。

    trace_complete 按"记录里带步骤级 trace 的占比"如实计算，
    不是写死的 100% —— 页面上那格数字必须经得起对账。

    only_user 的语义与 list_audits 一致，而且**必须一起收敛**：
    列表只给本人、统计却给全量，那张成本卡就是一个按天的聚合泄露 ——
    别人昨天花了多少、被拦了几次，一眼可见。同一道边界只做一半等于没做。
    """
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    recent: list[dict[str, Any]] = []
    for rec in read_records(path):
        if only_user is not None and (rec.get("user") or "") != only_user:
            continue
        t = _parse_ts(str(rec.get("ts", "")))
        if t is not None and t >= cutoff:
            recent.append(rec)

    calls = len(recent)
    blocked = sum(1 for r in recent if r.get("rejected_by"))
    with_steps = sum(1 for r in recent if r.get("steps"))
    elapsed = sorted(int(r.get("elapsed_ms") or 0) for r in recent)

    # 模型调用的成败按**节点**算，不是按整次调用算：一次提问里模型可能被调
    # 三四次（判定 / 生成 / 自检 / 反思），其中一次失败后重试成功，整次调用
    # 是成功的，但模型确实失败过一次。按调用算会把这些失败全部抹掉。
    model_steps = [s for r in recent for s in (r.get("steps") or [])
                   if s.get("step") in MODEL_STEPS]
    model_calls = len(model_steps)
    model_failed = sum(1 for s in model_steps if s.get("status") != "ok")

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
        # 窗口内一次模型节点都没有时为 None —— 0/0 不是 0%，也不是 100%
        "model_calls": model_calls,
        "model_failed": model_failed,
        "model_success": round((model_calls - model_failed) / model_calls, 4) if model_calls else None,
        "elapsed_p50_ms": _percentile(elapsed, 0.5),
        "elapsed_p95_ms": _percentile(elapsed, 0.95),
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "by_kind": by_kind,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["cost_cny"])),
    }


def _pctl_of(values: list[int], q: float) -> int | None:
    """按最近秩取分位。样本少时等于某个真实观测值 —— 见 _percentile 的说明。"""
    return _percentile(sorted(values), q)


def quality(path: Path, days: int = 1) -> dict[str, Any]:
    """线上运行质量：按**真实调用**算，不用黄金集分母。

    与 stats() 的分工：stats 服务审计页（流水、成本、按规则分布），
    这里服务质量中心 —— 多出来的是**按节点聚合**，那是设计稿里那张
    「工具/节点」表的数据来源，而它一直只能靠审计记录里的 steps 算出来。

    「成功」的口径写死在这里，不留解释空间：一次调用被护栏拦下（rejected_by
    非空）或执行失败，都算没成功。拦截是护栏干活、不是故障，所以两者分开报 ——
    把拦截混进失败率，会让"护栏越有效、质量看起来越差"。
    """
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    recent = [r for r in read_records(path)
              if (t := _parse_ts(str(r.get("ts", "")))) is not None and t >= cutoff]

    runs = len(recent)
    blocked = sum(1 for r in recent if r.get("rejected_by"))
    # 执行类失败（数据源异常、模型调用失败）与护栏拦截是两回事
    failed = sum(1 for r in recent if r.get("rejected_by") in ("EXEC", "LLM"))
    ok = runs - blocked

    elapsed = [int(r.get("elapsed_ms") or 0) for r in recent]
    tok = [int(r.get("tok_in") or 0) + int(r.get("tok_out") or 0) for r in recent]
    costs = [float(r.get("cost_cny") or 0) for r in recent]

    # ---- 按节点聚合 ----
    nodes: dict[str, dict[str, Any]] = {}
    for r in recent:
        for s in (r.get("steps") or []):
            name = str(s.get("step", ""))
            if not name:
                continue
            e = nodes.setdefault(name, {"calls": 0, "ok": 0, "ms": [], "tok": 0})
            e["calls"] += 1
            if s.get("status") == "ok":
                e["ok"] += 1
            e["ms"].append(int(s.get("ms") or 0))
            e["tok"] += int(s.get("tok_in") or 0) + int(s.get("tok_out") or 0)

    node_rows = [
        {
            "step": name,
            "calls": e["calls"],
            "success_rate": round(e["ok"] / e["calls"], 4) if e["calls"] else None,
            "p50_ms": _pctl_of(e["ms"], 0.5),
            "p95_ms": _pctl_of(e["ms"], 0.95),
            "tok": e["tok"],
        }
        for name, e in nodes.items()
    ]
    # 按 P95 倒序：这张表是拿来找延迟贡献最大的那一段的
    node_rows.sort(key=lambda d: (d["p95_ms"] or 0), reverse=True)

    return {
        "days": days,
        "runs": runs,
        "ok": ok,
        "blocked": blocked,
        "failed": failed,
        # 成功率的分母是全部调用；拦截单列，不混进失败
        "success_rate": round(ok / runs, 4) if runs else None,
        "block_rate": round(blocked / runs, 4) if runs else None,
        "p50_ms": _pctl_of(elapsed, 0.5),
        "p95_ms": _pctl_of(elapsed, 0.95),
        "avg_tok": round(sum(tok) / runs) if runs else None,
        "cost_cny": round(sum(costs), 6),
        "avg_cost_cny": round(sum(costs) / runs, 6) if runs else None,
        # 拦截按规则分布，多的在前 —— 这张表回答的是"护栏主要在挡什么"
        "by_rule": dict(sorted(
            Counter(str(r["rejected_by"]) for r in recent if r.get("rejected_by")).items(),
            key=lambda kv: -kv[1])),
        "nodes": node_rows,
    }
