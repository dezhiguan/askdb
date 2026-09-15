"""Agent 链路对照组 —— 改了 agent 流程之后，判断"是变好了还是变坏了"。

**为什么要有这个东西。**
2026-09-14 改 agent 决策流程（撤掉冗余工具轮次）之后，我们手上没有任何能回答
"改好了还是改坏了"的东西：单元测试验的是接线和护栏，不是业务正确性；线上只
手跑了一条查询。而 `evals/run_live.py` 那条路自 2026-09-10「查询要登录」上线
后就一直在对着 401 打分 —— `evals/results/careermate-blind.json` 里那个
`accuracy=0.0 / block_rate=1.0` 是登录问题，不是产品退化。

用例不是编的，全部取自**生产上真实发生过、且在改动之前就成功**的复杂链路
（见 baseline-cases.jsonl 里的 from_trace / from_ts）。

**首份基线不能用来给那次改动定性。** 2026-09-14 当天 15:54 又上了两个提交
（6cff407 R-11 扫描量估算、58cd176 接地校验第二档 shadow 旋钮），而首轮 15:55
才开跑 —— 它测到的是三者叠加后的生产，归因不到任何单独一个改动。这份基线的
价值在**往后**：下一次改 agent 之前跑一轮，改完再跑一轮，两份一比就有答案。
results/baseline.json 的 build / note 字段记着每一轮对应哪些提交，别丢。

两层指标，各管各的
------------------
**L1 · 链路健康**（本模块的主体，零人工标注）
    出没出结果、被哪条规则拦了、跑了几步、烧了多少 token、花了多少钱、多久。
    它抓的是"链路坏了"：原先有答案的问题现在交白卷，是最该第一时间看见的退化。

**L2 · 答案正确**（用例里 expect_sql 非空的那些才参与）
    标准 SQL 交给同一个站点的 /api/sql 跑一遍，行集比对。

    **为什么 expect_sql 是人工定稿、而不是从历史 trace 里自动扒的**：真实
    trace 里存在"链路成功但答非所问"的案例 —— 线上 7583fc6705d5 问「各类目下
    分别有多少商品」，跑成功的 SQL 是
    `SELECT category_id, category_name, level, full_path FROM categories`，
    一个商品数都没统计。把历史成功当标准答案，等于把错答固化成基线。
    另外审计里的 rows_returned 取的是**最后执行**那条 SQL 的行数，不是回答问题
    那条（见 askdb 已知问题），拿它反查答案 SQL 同样会选中探查语句。

跑法
----
    # 登录信息只从环境变量取，脚本不落盘、不打印
    ASKDB_USER=... ASKDB_PASSWORD=... python -m evals.baseline
    python -m evals.baseline --compare      # 不跑，只对比已有的两份结果

结果写 evals/results/baseline.json，上一份自动转存 baseline.prev.json ——
与 careermate-blind / ragforge-blind 同一套前后对照惯例。

用 curl 而不是 requests/urllib：本机 python 没有 CA 根证书，urllib 直接在 TLS
握手断掉（run_live.py 踩过同一个坑）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

BASE = os.environ.get("ASKDB_BASE", "https://askdb.ragforge.net")
HERE = Path(__file__).resolve().parent
CASES = HERE / "baseline-cases.jsonl"
OUT = HERE / "results" / "baseline.json"

#: /api/ask 入口限流 20/min（deploy/nginx-askdb.conf）。留出余量，别让对照组
#: 自己把自己打成 429 —— 那会被记成"链路退化"，而真相是跑得太快。
SLEEP_S = 4.0


def _curl(args: list[str]) -> str:
    return subprocess.run(["curl", "-s", *args], capture_output=True, text=True).stdout


def _git_head() -> str:
    """当前 HEAD 的短 sha —— 落进结果里当"这一轮跑的是哪版代码"。

    取不到就返回空串，不猜也不抛：这是标注字段，不该让它把一轮跑测弄崩。
    """
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=HERE.parent, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                          # noqa: BLE001
        return ""


def login(jar: str) -> str:
    """登录并把会话 cookie 存进 jar。返回空串表示成功，否则返回失败原因。

    口令只经环境变量进 curl 的 --data，不写进结果文件、不进日志。
    未配置账号时直接返回原因而不是抛错：匿名也能跑 --compare。
    """
    user, pwd = os.environ.get("ASKDB_USER"), os.environ.get("ASKDB_PASSWORD")
    if not user or not pwd:
        return "未配置 ASKDB_USER / ASKDB_PASSWORD"
    out = _curl(["-m", "30", "-c", jar, "-X", "POST", f"{BASE}/api/auth/login",
                 "-H", "Content-Type: application/json",
                 "--data", json.dumps({"username": user, "password": pwd})])
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return f"登录响应不是 JSON：{out[:120]}"
    return "" if d.get("ok") or d.get("user") else f"登录失败：{str(d)[:160]}"


def ask(jar: str, case: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    """问一题，**并跟进长任务交接**。

    不跟进交接就什么都测不到：/api/ask 在耗时超阈值或预估扫描量大时直接返回
    `{"async": true, "thread_id": ...}` 这样一张回执，结果要到 /api/tasks/{id}
    去取。而交接是**常态路径不是异常路径**（server.task_detail 的注释：阈值降到
    10 秒之后实测 p50 9.6s）。拿回执当结果，整份基线会是一排"无答案"的假退化 ——
    第一版这么写过，23 条第一条就踩中。
    """
    out = _curl(["-m", str(timeout), "-b", jar, "-X", "POST", f"{BASE}/api/ask",
                 "-H", "Content-Type: application/json",
                 "--data", json.dumps({"question": case["question"],
                                       "source": case["source"]},
                                      ensure_ascii=False)])
    try:
        r = json.loads(out)
    except json.JSONDecodeError:
        # 不静默：一轮跑完之后"这题为什么失败"要能从结果文件里看出来。
        return {"ok": False, "rejected_by": "HTTP", "error": (out or "空响应")[:200]}
    if not (r.get("async") and r.get("thread_id")):
        return r
    tid = r["thread_id"]
    for _ in range(60):                       # 最多等 5 分钟
        time.sleep(5)
        try:
            td = json.loads(_curl(["-m", "30", "-b", jar, f"{BASE}/api/tasks/{tid}"]))
        except json.JSONDecodeError:
            continue
        stage = td.get("status") or ""
        if stage and stage != "running":
            res = td.get("result") or {
                "ok": False, "rejected_by": td.get("rejected_by") or "",
                "error": td.get("error") or "", "trace_id": tid}
            res["__stage"] = stage
            res["__handed"] = True
            return res
    return {"ok": False, "rejected_by": "TIMEOUT", "trace_id": tid, "__handed": True,
            "error": "交接后 5 分钟未收尾"}


def run_sql(jar: str, source: str, sql: str, timeout: int = 60) -> dict[str, Any]:
    out = _curl(["-m", str(timeout), "-b", jar, "-X", "POST", f"{BASE}/api/sql",
                 "-H", "Content-Type: application/json",
                 "--data", json.dumps({"sql": sql, "source": source}, ensure_ascii=False)])
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": (out or "空响应")[:200]}


def judge_l2(jar: str, case: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    """L2：标准 SQL 交给**同一个站点**跑，行集比对。

    标准答案不预存成固定行，而是每次重新执行 —— 数据会变，冻结下来的期望值早晚
    与库对不上，那时失败的是基线不是产品。判定直接复用 replay 那套（_rows_match
    按集合比、_redundant_cols 认"顺手多给一列上下文"），不另起一套标准：两份成绩
    用不同尺子量出来就不可比，而不可比的成绩没有意义。
    """
    from .replay import _redundant_cols, _rows_match

    sql = (case.get("expect_sql") or "").strip()
    if not sql:
        return {"l2": ""}                      # 没定稿标准答案的用例不参与 L2
    exp = run_sql(jar, case["source"], sql)
    if not exp.get("ok"):
        # 标准 SQL 自己跑不通是**基线的问题**，不是产品答错，必须与 fail 分开。
        return {"l2": "expect_broken", "l2_detail": (exp.get("error") or "")[:120]}
    want = [tuple(x) for x in (exp.get("rows") or [])]
    got = [tuple(x) for x in (r.get("rows") or [])]
    if _rows_match(got, want):
        return {"l2": "pass"}
    extra = _redundant_cols(got, want)
    if extra is not None:
        return {"l2": "pass_redundant",
                "l2_detail": f"答案含标准答案，另多带 {len(extra)} 列上下文"}
    return {"l2": "fail",
            "l2_detail": f"期望 {len(want)} 行，实得 {len(got)} 行"}


def measure(case: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    """把一次真实执行折成对照组关心的那几维。

    answered 的判据是"**拿到了可用的产出**"，不是 ok 这一个布尔：交接成异步任务
    时 ok 为真但当场没有结果，那种情况要单独看得见，不能混进"有答案"。
    """
    steps = r.get("steps") or []
    rows = r.get("rows") or []
    return {
        "id": case["id"],
        "source_name": case["source_name"],
        "question": case["question"],
        "answered": bool(r.get("ok")) and (bool(rows) or bool(r.get("reasoning"))),
        # 走没走交接、落在哪一档。**交接本身不是失败**，它是常态路径；
        # 但 waiting_approval / interrupted 要与"跑完了没答案"分开看 —— 前者是
        # 护栏按设计挂了单（R-11 大扫描），后者才是链路退化。
        "handoff": bool(r.get("__handed")),
        "stage": r.get("__stage") or "",
        "rejected_by": r.get("rejected_by") or "",
        "rows": len(rows),
        "steps": len([s for s in steps if s.get("step") == "decide"]) or r.get("step_count") or 0,
        "tool_calls": len([s for s in steps if s.get("step") == "tool_call"]),
        "tok": sum((s.get("tok_in") or 0) + (s.get("tok_out") or 0) for s in steps),
        "cached_in": sum(s.get("cached_in") or 0 for s in steps),
        "cost_cny": round(float(r.get("cost_cny") or 0), 6),
        "elapsed_ms": int(r.get("elapsed_ms") or 0),
        "trace_id": r.get("trace_id") or "",
        # 参照系：这条用例当初（改动之前）真实跑成什么样。
        "ref_steps": case.get("ref_steps"),
        "ref_cost_cny": case.get("ref_cost_cny"),
        "ref_elapsed_ms": case.get("ref_elapsed_ms"),
        "from_trace": case.get("from_trace"),
    }


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items) or 1
    ok = [x for x in items if x["answered"]]
    lat = sorted(x["elapsed_ms"] for x in items)
    return {
        "n": len(items),
        "answered_rate": round(len(ok) / n, 4),
        "blocked_rate": round(len([x for x in items if x["rejected_by"]]) / n, 4),
        "handoff_rate": round(len([x for x in items if x["handoff"]]) / n, 4),
        "stages": _count(x.get("stage") or "" for x in items if x.get("stage")),
        "avg_steps": round(sum(x["steps"] for x in items) / n, 2),
        "avg_tool_calls": round(sum(x["tool_calls"] for x in items) / n, 2),
        "avg_tok": round(sum(x["tok"] for x in items) / n),
        "cache_hit_rate": round(
            sum(x["cached_in"] for x in items) / max(1, sum(x["tok"] for x in items)), 4),
        "avg_cost_cny": round(sum(x["cost_cny"] for x in items) / n, 6),
        "total_cost_cny": round(sum(x["cost_cny"] for x in items), 4),
        "p50_ms": lat[len(lat) // 2] if lat else 0,
        "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0,
        "rejected_kinds": _count(x["rejected_by"] for x in items if x["rejected_by"]),
        # L2 只在定稿了标准答案的那几条上算 —— 拿全量当分母会把"还没标注"
        # 混进"答错"，越标越难看。
        "l2_n": len([x for x in items if x.get("l2")]),
        "l2_pass_rate": (round(len([x for x in items
                                    if x.get("l2") in ("pass", "pass_redundant")])
                               / max(1, len([x for x in items if x.get("l2")])), 4)
                         if any(x.get("l2") for x in items) else None),
        "l2_kinds": _count(x["l2"] for x in items if x.get("l2")),
    }


def _count(xs) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def compare(cur: dict[str, Any], prev: dict[str, Any]) -> None:
    """两份结果的差。**先报逐条的状态翻转，再报聚合指标。**

    顺序是有意的：聚合数字会把"三条变好、三条变坏"抹成"没变化"，而对照组最该
    第一眼看见的恰恰是"原先有答案的这几条现在交白卷了"。
    """
    pm = {x["id"]: x for x in prev.get("items", [])}
    reg = [x for x in cur["items"] if x["id"] in pm
           and pm[x["id"]]["answered"] and not x["answered"]]
    fix = [x for x in cur["items"] if x["id"] in pm
           and not pm[x["id"]]["answered"] and x["answered"]]
    print("\n【逐条状态翻转】")
    for x in reg:
        print(f"  ✗ 退化 {x['id']} {x['source_name']} · {x['question'][:28]}"
              f" → {x['rejected_by'] or '无答案'}  ({x['trace_id']})")
    for x in fix:
        print(f"  ✓ 修复 {x['id']} {x['source_name']} · {x['question'][:28]}")
    l2reg = [x for x in cur["items"] if x["id"] in pm
             and pm[x["id"]].get("l2") in ("pass", "pass_redundant")
             and x.get("l2") == "fail"]
    for x in l2reg:
        print(f"  ✗ 答案退化 {x['id']} {x['source_name']} · {x.get('l2_detail','')}")
    if not reg and not fix and not l2reg:
        print("  （无翻转）")

    print("\n【聚合指标】")
    a, b = cur["summary"], prev.get("summary", {})
    for k in ("answered_rate", "blocked_rate", "avg_steps", "avg_tool_calls",
              "avg_tok", "cache_hit_rate", "avg_cost_cny", "p50_ms", "p95_ms"):
        av, bv = a.get(k), b.get(k)
        if bv in (None, 0):
            print(f"  {k:<16} {av}")
            continue
        d = (av - bv) / bv * 100
        print(f"  {k:<16} {bv}  →  {av}   ({d:+.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent 链路对照组")
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--only", default="", help="只跑某个数据源（按名字子串匹配）")
    ap.add_argument("--sleep", type=float, default=SLEEP_S)
    ap.add_argument("--compare", action="store_true", help="不跑，只对比已有两份结果")
    ap.add_argument("--note", default="", help="记进结果的一句话：这一轮对应哪些改动")
    a = ap.parse_args()

    out_p, prev_p = Path(a.out), Path(a.out).with_suffix(".prev.json")
    if a.compare:
        if not out_p.exists() or not prev_p.exists():
            print("缺少 baseline.json 或 baseline.prev.json，先跑一轮")
            return 2
        compare(json.loads(out_p.read_text()), json.loads(prev_p.read_text()))
        return 0

    cases = [json.loads(x) for x in Path(a.cases).read_text().splitlines() if x.strip()]
    if a.only:
        cases = [c for c in cases if a.only in c["source_name"]]
    if not cases:
        print("没有用例")
        return 2

    with tempfile.TemporaryDirectory() as td:
        jar = str(Path(td) / "cookies")
        why = login(jar)
        if why:
            print(f"登录不可用：{why}\n"
                  f"  /api/ask 自 2026-09-10 起需要登录，匿名跑出来的 401 会被记成"
                  f"「全部被拦」—— 那是假的退化，不要拿它当基线。")
            return 2
        items = []
        for i, c in enumerate(cases, 1):
            r = ask(jar, c)
            m = measure(c, r)
            m.update(judge_l2(jar, c, r))
            items.append(m)
            flag = "✓" if m["answered"] else ("⏸" if m["handoff"] else "✗")
            print(f"[{i:02}/{len(cases)}] {flag} {c['source_name']:<16}"
                  f" {m['steps']}步 {m['tok']:>6}tok ¥{m['cost_cny']:.4f}"
                  f" {m['elapsed_ms']:>6}ms  {c['question'][:26]}"
                  + (f"  ← {m['rejected_by']}" if m["rejected_by"] else ""))
            time.sleep(a.sleep)

    # build / note **必须真的写进去**。模块头写着"记着每一轮对应哪些提交，别丢"，
    # 而在此之前没有任何代码写这两个字段 —— 首份基线里那两行是手工填的。
    # 一个文档声明承重、工具却产不出来的字段，等于每一轮都在悄悄丢掉归因：
    # 两份结果摆在一起时，没人说得出它们各自跑的是哪版代码。
    res = {"base": BASE, "build": _git_head(), "note": a.note,
           "n_cases": len(items), "summary": summarize(items), "items": items}
    out_p.parent.mkdir(parents=True, exist_ok=True)
    if out_p.exists():
        prev_p.write_text(out_p.read_text())
    out_p.write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n写入 {out_p}" + (f"（上一份已转存 {prev_p.name}）" if prev_p.exists() else ""))
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    if prev_p.exists():
        compare(res, json.loads(prev_p.read_text()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
