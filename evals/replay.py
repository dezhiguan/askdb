"""黄金集回放（技术设计说明书 §6.2）。

指标定义：
  执行准确率  —— 生成 SQL 的执行结果集与标准结果集一致的题目占比。
                列顺序无关；未显式排序时按集合比对。
  误拒率      —— 正确 SQL 被护栏错误拦截的比例（衡量护栏是否过严）。
  拦截命中率  —— 应被拒绝的题目里，确实被拒且规则编号正确的比例。
  多步误用率  —— 本应单步却走了多步的比例。必须与多跳准确率同时报告，
                否则可以靠"所有题都走多步"把多跳成绩刷上去。

指标须成对观察：单独优化准确率可以通过放宽护栏实现，
单独优化拦截率可以通过全部拒绝实现 —— 任一单点指标都可被操纵。

用法：
  python -m evals.replay                      # 仅非盲测题
  python -m evals.replay --blind              # 仅盲测题（验收用，成绩即最终成绩）
  python -m evals.replay --all --out r.json
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from askdb import graph, guard
from askdb.config import Config, load as load_cfg
from askdb.executor import Executor

from .golden import Case, load as load_cases

HERE = Path(__file__).resolve().parent


@dataclass
class Outcome:
    id: str
    category: str
    blind: bool
    passed: bool
    reason: str = ""            # 失败原因分类
    detail: str = ""
    trace_id: str = ""          # 供 graph.replay() 原样复现
    steps: int = 1
    misused_multi: bool = False
    elapsed_ms: int = 0
    tok_in: int = 0
    tok_out: int = 0
    cost_cny: float = 0.0


@dataclass
class Report:
    group: str
    n: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    # ---- 指标 ----
    def _sel(self, **kw) -> list[Outcome]:
        return [o for o in self.outcomes
                if all(getattr(o, k) == v for k, v in kw.items())]

    @property
    def answerable(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.category != "reject"]

    @property
    def accuracy(self) -> float:
        a = self.answerable
        return round(sum(o.passed for o in a) / len(a), 4) if a else 0.0

    @property
    def false_reject(self) -> float:
        """正确题被护栏拦下的比例。"""
        a = self.answerable
        if not a:
            return 0.0
        bad = sum(1 for o in a if o.reason == "被护栏拦截")
        return round(bad / len(a), 4)

    @property
    def block_rate(self) -> float:
        r = self._sel(category="reject")
        return round(sum(o.passed for o in r) / len(r), 4) if r else 0.0

    @property
    def multi_misuse(self) -> float:
        h = self._sel(category="multihop")
        return round(sum(o.misused_multi for o in h) / len(h), 4) if h else 0.0

    @property
    def avg_steps(self) -> float:
        return round(sum(o.steps for o in self.outcomes) / max(len(self.outcomes), 1), 2)

    @property
    def cost(self) -> float:
        return round(sum(o.cost_cny for o in self.outcomes), 4)

    @property
    def p95_ms(self) -> int:
        xs = sorted(o.elapsed_ms for o in self.outcomes)
        return xs[min(int(len(xs) * 0.95), len(xs) - 1)] if xs else 0

    @property
    def failure_kinds(self) -> dict[str, int]:
        """失败分类分布 —— 不做筛选，全量公开。"""
        return dict(Counter(o.reason for o in self.outcomes if not o.passed))

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group, "n": self.n,
            "accuracy": self.accuracy, "false_reject": self.false_reject,
            "block_rate": self.block_rate, "multi_misuse": self.multi_misuse,
            "avg_steps": self.avg_steps, "cost_cny": self.cost, "p95_ms": self.p95_ms,
            "failure_kinds": self.failure_kinds,
            "outcomes": [asdict(o) for o in self.outcomes],
        }


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

def _norm(rows: list[list[Any]]) -> set[tuple]:
    """结果集规范化：按集合比对，抹平行序与数值表示差异。"""
    out = set()
    for r in rows:
        cells = []
        for v in r:
            if isinstance(v, float):
                cells.append(round(v, 4))
            elif v is None:
                cells.append(None)
            else:
                cells.append(str(v))
        out.add(tuple(cells))
    return out


def _expected(case: Case, cfg: Config, ex: Executor) -> set[tuple] | None:
    """标准结果集。

    **标准 SQL 必须走同一套护栏再执行。** 否则它拿到的是全租户、无 LIMIT
    的结果，而被判定的答案是注入了租户谓词与 LIMIT 之后的结果 ——
    比的是两个不同东西，准确率会恒为 0。
    """
    if not case.expect_sql:
        return None
    g = guard.check(case.expect_sql, cfg, org_id=cfg.default_org, dialect=cfg.dialect)
    if not g.ok:
        raise RuntimeError(f"标准 SQL 未能通过护栏（{g.rejected_by}：{g.reason}）")
    return _norm(ex.run(g.sql).rows)


def judge(case: Case, r: graph.AskResult, cfg: Config, ex: Executor) -> Outcome:
    o = Outcome(id=case.id, category=case.category, blind=case.blind, passed=False,
                trace_id=r.trace_id, steps=r.step_count, elapsed_ms=r.elapsed_ms,
                tok_in=r.tok_in, tok_out=r.tok_out, cost_cny=r.cost_cny)

    if case.kind == "reject":
        if r.ok:
            o.reason, o.detail = "应拒未拒", f"返回了 {r.row_count} 行"
        elif case.expect_rule and r.rejected_by != case.expect_rule:
            # 拦住了但规则不对 —— 记为通过但标注，安全性达标、归因不准
            o.passed = True
            o.reason = "拦截规则不符"
            o.detail = f"期望 {case.expect_rule}，实际 {r.rejected_by}"
        else:
            o.passed = True
        return o

    if not r.ok:
        o.reason = "被护栏拦截" if (r.rejected_by or "").startswith("R-") else "链路失败"
        o.detail = f"{r.rejected_by}｜{r.error[:120]}"
        return o

    if case.category == "multihop":
        o.misused_multi = bool(case.should_be_single and r.step_count > 1)
        if case.min_rows <= r.row_count <= case.max_rows:
            o.passed = True
        else:
            o.reason, o.detail = "行数超出预期区间", f"{r.row_count} 行"
        return o

    if case.kind == "shape":
        o.passed = case.min_rows <= r.row_count <= case.max_rows
        if not o.passed:
            o.reason, o.detail = "行数超出预期区间", f"{r.row_count} 行"
        return o

    try:
        exp = _expected(case, cfg, ex)
    except Exception as e:
        # 标准 SQL 自己过不了护栏 —— 这是评测集的问题，必须显式记下来，
        # 不能算模型失败，更不能中断整轮
        o.reason, o.detail = "标准答案不可用", str(e)[:140]
        return o
    if exp is None:
        o.passed = True
        return o
    got = _norm(r.rows)
    if got == exp:
        o.passed = True
    else:
        o.reason = "结果不一致"
        o.detail = f"期望 {len(exp)} 行，实得 {len(got)} 行"
    return o


# --------------------------------------------------------------------------
# 回放
# --------------------------------------------------------------------------

def run(cfg: Config, cases: list[Case], group: str = "current",
        verbose: bool = True) -> Report:
    rep = Report(group=group, n=len(cases))
    with Executor(cfg) as ex:
        for i, c in enumerate(cases, 1):
            t0 = time.perf_counter()
            try:
                r = graph.ask(c.question, cfg, executor=ex)
            except Exception as e:      # 链路本身崩了也要记，不能中断整轮
                rep.outcomes.append(Outcome(
                    id=c.id, category=c.category, blind=c.blind, passed=False,
                    reason="链路异常", detail=str(e)[:160],
                    elapsed_ms=int((time.perf_counter() - t0) * 1000)))
                continue
            o = judge(c, r, cfg, ex)
            rep.outcomes.append(o)
            if verbose:
                mark = "✓" if o.passed else "✗"
                extra = f"  {o.reason}" if o.reason else ""
                print(f"  [{i:>2}/{len(cases)}] {mark} {c.id} {c.category:<9}"
                      f"{c.question[:26]:<28}{o.elapsed_ms:>6}ms{extra}")
    return rep


def summarize(rep: Report) -> str:
    lines = [
        f"\n{'=' * 62}",
        f"  {rep.group}  共 {rep.n} 题",
        f"{'=' * 62}",
        f"  执行准确率      {rep.accuracy:.1%}   （可作答题 {len(rep.answerable)} 道）",
        f"  误拒率          {rep.false_reject:.1%}   越低越好，与准确率必须一起看",
        f"  应拒拦截率      {rep.block_rate:.1%}",
        f"  多步误用率      {rep.multi_misuse:.1%}   本应单步却走了多步",
        f"  平均步数        {rep.avg_steps}",
        f"  P95 延迟        {rep.p95_ms} ms",
        f"  总成本          ¥{rep.cost}",
    ]
    if rep.failure_kinds:
        lines.append("  失败分类（不做筛选，全量列出）：")
        for k, v in sorted(rep.failure_kinds.items(), key=lambda x: -x[1]):
            lines.append(f"    {k:<16}{v}")
        lines.append("  失败样本可用 trace_id 原样复现：askdb replay <trace_id>")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="黄金集回放")
    ap.add_argument("-c", "--config", default="config/askdb.yaml")
    ap.add_argument("--blind", action="store_true", help="仅盲测集（验收用）")
    ap.add_argument("--all", action="store_true", help="全部题目")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题，便于冒烟")
    ap.add_argument("--out", default="", help="结果写入 JSON")
    a = ap.parse_args()

    cfg = load_cfg(a.config)
    cases = load_cases()
    if a.blind:
        cases, group = [c for c in cases if c.blind], "盲测集（最终成绩）"
    elif a.all:
        group = "全集"
    else:
        cases, group = [c for c in cases if not c.blind], "非盲测集（调参用）"
    if a.limit:
        cases = cases[: a.limit]

    print(f"回放 {group}：{len(cases)} 题  配置 {a.config}")
    rep = run(cfg, cases, group=group)
    print(summarize(rep))

    if a.out:
        Path(a.out).write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"\n结果已写入 {a.out}")


if __name__ == "__main__":
    main()
