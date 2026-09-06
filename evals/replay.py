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
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from decimal import Decimal
from collections.abc import Callable
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
    #: 生成的最终 SQL。口径命中要拿它比对，没有它这项事后无从复算
    sql_final: str = ""
    #: 本题召回阶段注入了哪些认证口径
    metrics_injected: list[str] = field(default_factory=list)
    #: 认证口径有没有真的进最终 SQL。None = 本题没注入带定义式的口径，不进分母
    metric_used: bool | None = None
    metric_detail: str = ""
    #: 结果能不能直接拿来作答。None = 本题压根没跑出结果集，不进分母
    complete: bool | None = None
    incomplete_why: str = ""


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
    def quota_blocked(self) -> list[Outcome]:
        """因每日配额触顶而根本没跑起来的题。

        这类失败与模型能力无关 —— 请求在任何模型调用之前就被拦下了。
        历史上出现过一轮消融里 23 题被配额拒绝，全部计入分母，
        直接把该组成绩压低了一大截且偏差不可估。
        设计 §6.2 把执行准确率定义为"生成 SQL 执行结果集与标准答案一致的
        题目占比"，未提及配额；按此定义，没生成过 SQL 的题不该进分母。
        """
        return [o for o in self.outcomes if o.reason == "配额拒绝"]

    @property
    def answerable(self) -> list[Outcome]:
        """计入准确率的题：排除应拒题，也排除配额触顶未跑成的题。"""
        blocked = {id(o) for o in self.quota_blocked}
        return [o for o in self.outcomes
                if o.category != "reject" and id(o) not in blocked]

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
    def avg_tok(self) -> int:
        """每道题的平均 token（输入 + 输出）。

        分母是**跑过的题**，不是答对的题：一道题失败照样烧了 token，
        把它从分母里去掉，平均值就成了"顺利时的花费"，用来估成本会偏低。
        """
        if not self.outcomes:
            return 0
        return round(sum(o.tok_in + o.tok_out for o in self.outcomes) / len(self.outcomes))

    @property
    def p95_ms(self) -> int:
        xs = sorted(o.elapsed_ms for o in self.outcomes)
        return xs[min(int(len(xs) * 0.95), len(xs) - 1)] if xs else 0

    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def complete_graded(self) -> list[Outcome]:
        """能判结果完整度的题：真的跑出了结果集的那些。

        被拒、链路失败的题没有结果可判，进分母就成了拿"没跑出来"去压
        "跑出来但不完整" —— 那两件事已经分别由拦截率和准确率在报了。
        """
        return [o for o in self.outcomes if o.complete is not None]

    @property
    def completeness(self) -> float | None:
        """结果完整度：结果集可直接作答的比例。None = 本轮没有判得动的题。"""
        g = self.complete_graded
        return round(sum(bool(o.complete) for o in g) / len(g), 4) if g else None

    @property
    def metric_graded(self) -> list[Outcome]:
        """能判口径命中的题：召回真的注入了带定义式的口径，且生成了 SQL。

        没注入口径的题不能进分母 —— 模型完全无视口径也答得对的题混进来，
        算出的"命中率"测不出任何东西，只会被题目构成推着走。
        """
        return [o for o in self.outcomes if o.metric_used is not None]

    @property
    def metric_hit_rate(self) -> float | None:
        """业务口径命中率。None = 本轮没有一道题判得动，不是 0。"""
        g = self.metric_graded
        return round(sum(bool(o.metric_used) for o in g) / len(g), 4) if g else None

    @property
    def failure_kinds(self) -> dict[str, int]:
        """失败分类分布 —— 不做筛选，全量公开。"""
        return dict(Counter(o.reason for o in self.outcomes if not o.passed))

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group, "n": self.n,
            # 被配额拦掉的题数必须显式带出来 —— 悄悄从分母里去掉，
            # 和悄悄留在分母里，是同一种不诚实
            "quota_blocked": len(self.quota_blocked),
            # 成绩必须带出处。没有这几项，一份结果文件事后无从分辨
            # 它跑在哪个库、哪个题库、哪个模型上 —— 而这恰恰决定了
            # 这个数字能不能拿来说事。
            "provenance": self.provenance,
            "accuracy": self.accuracy, "false_reject": self.false_reject,
            "block_rate": self.block_rate, "multi_misuse": self.multi_misuse,
            "avg_steps": self.avg_steps, "cost_cny": self.cost, "p95_ms": self.p95_ms,
            "avg_tok": self.avg_tok,
            "metric_hit_rate": self.metric_hit_rate,
            "metric_graded_n": len(self.metric_graded),
            "completeness": self.completeness,
            "complete_graded_n": len(self.complete_graded),
            "failure_kinds": self.failure_kinds,
            "outcomes": [asdict(o) for o in self.outcomes],
        }


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

# 数值相对容差。选 1e-4 的依据：
#   · 需要抹平的是**舍入差**——标准 SQL 写 ROUND(AVG(x),1) 而模型写 AVG(x)，
#     实测 233.4954 vs 233.5，相对差 2e-5；口径题 0.372710 vs 0.3727 差 3e-5。
#     这类差异对"平均耗时是多少"这个问题而言，两个答案是同一个答案。
#   · 需要**保留**的是口径错——把日均成本的分母写成行数而非天数，
#     结果 0.1656 vs 0.3727，相对差 55%。
# 1e-4 落在两者之间，离两边各有三个数量级以上的余量，不是照着某几道题调出来的。
NUM_RTOL = 1e-4


def _num(v: Any) -> float | None:
    """能当数值比的就当数值比。

    三件事必须一起处理，少一件这层容差就形同虚设：

    1. Decimal —— PostgreSQL 的聚合结果几乎全是 Decimal。
    2. **数值字符串** —— 这是最隐蔽的一处：被判定的答案来自 graph.ask，
       已被 jsonable() 转成字符串（'233.4875…'），而标准答案是直连
       Executor 取的原始 Decimal（233.5）。两边类型不同，数值分支根本
       进不去，容差写了也不生效。
    3. bool 排除在外 —— True 与 1 不该被视作同一个答案。
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None       # 日期、名称等一律回落到字符串比对
    return None


def _cell_eq(a: Any, b: Any) -> bool:
    x, y = _num(a), _num(b)
    if x is not None and y is not None:
        return x == y or abs(x - y) <= NUM_RTOL * max(abs(x), abs(y), 1e-12)
    if (a is None) != (b is None):
        return False
    return str(a) == str(b)


def _row_eq(a: tuple, b: tuple) -> bool:
    return len(a) == len(b) and all(_cell_eq(x, y) for x, y in zip(a, b))


def _norm(rows: list[list[Any]]) -> list[tuple]:
    """结果集规范化 —— 只做行内定形，数值比对交给 _rows_match。

    返回列表而非集合：带容差的比对无法用哈希表达（容差不满足传递性，
    做不出稳定的哈希键），只能逐行配对。结果集有 R-13 的行数上限兜底，
    O(n²) 配对不会失控。
    """
    return [tuple(r) for r in rows]


def _rows_match(got: list[tuple], exp: list[tuple]) -> bool:
    """按集合比对：行序无关，但要求一一对应（不是子集，也不去重）。"""
    if len(got) != len(exp):
        return False
    pool = list(exp)
    for g in got:
        for i, e in enumerate(pool):
            if _row_eq(g, e):
                pool.pop(i)
                break
        else:
            return False
    return True


def _expected(case: Case, cfg: Config, ex: Executor) -> list[tuple] | None:
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


def _sql_norm(s: str) -> str:
    """SQL 归一化到可做子串比对的形态：小写 + 空白折叠。

    只抹掉换行与缩进这类无意义差异，**不做语义解析** —— 判定必须是确定性的，
    看得懂、复算得出来，否则这个数字和让模型自己打分没有区别。
    """
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _metric_adherence(r: graph.AskResult, cfg: Config) -> tuple[bool | None, str, list[str]]:
    """认证口径有没有真的进最终 SQL。

    口径存在的全部意义就是"按定义算，别凭直觉算"：`expr` 是认证定义式，
    `naive` 是与之对照的直觉写法。判定规则：

      · 召回没注入带 expr 的口径，或压根没生成 SQL → 返回 None，本题不进分母。
      · 注入的口径里**任意一条**的 expr 出现在最终 SQL 里 → 命中。
        取"任意一条"而不是"全部"，是因为召回按 max_metrics 一次最多塞 2 条，
        其中常有一条与本题无关 —— 要求全部出现会把无关口径记成失分。
      · 一条都没出现 → 未命中；若 SQL 里反而出现了某条口径的 naive 写法，
        额外记下来：那正是口径要防的那件事，光说"没用上"会漏掉这层信息。

    返回 (命中, 说明, 参与判定的口径名)。
    """
    names = list(r.metrics_hit or [])
    if not names or not r.sql_final:
        return None, "", []
    by_name = {m.name: m for m in cfg.metrics}
    graded = [by_name[n] for n in names if by_name.get(n) and by_name[n].expr]
    if not graded:
        return None, "", []

    sql = _sql_norm(r.sql_final)
    if any(_sql_norm(m.expr) in sql for m in graded):
        return True, "", [m.name for m in graded]

    detail = "未用口径定义式：" + "、".join(m.name for m in graded)
    naive_used = [m.name for m in graded if m.naive and _sql_norm(m.naive) in sql]
    if naive_used:
        detail += "；改用了直觉写法：" + "、".join(naive_used)
    return False, detail, [m.name for m in graded]


def _completeness(r: graph.AskResult) -> tuple[bool | None, str]:
    """结果集能不能直接拿来作答。

    askdb 不写自然语言结论（链路里没有 summarize 节点），所以"结论有没有超出
    结果集"这件事无从谈起。同一层意思在这个产品形态下能测的是**反过来的一问**：
    交回去的这份结果，本身够不够回答那个问题。三种情况算不够 ——

      · truncated       —— 被 LIMIT 截断，看到的不是全集
      · converged_early —— 多步撞上步数/成本上限，基于半截结果收敛
      · recall_blind    —— 召回是盲选，给模型的表不是按相关度挑的，可能答非所问

    这三项都是链路自己记下来的**事实标记**，不是判断，所以这个数是确定性的。
    没跑出结果集的题返回 None，不进分母。
    """
    if not r.ok:
        return None, ""
    why = []
    if r.truncated:
        why.append("结果被截断")
    if r.converged_early:
        why.append(f"提前收敛（{r.converged_early}）")
    if r.recall_blind:
        why.append("召回盲选" + (f"（{r.recall_note}）" if r.recall_note else ""))
    return (not why), "；".join(why)


def judge(case: Case, r: graph.AskResult, cfg: Config, ex: Executor) -> Outcome:
    o = Outcome(id=case.id, category=case.category, blind=case.blind, passed=False,
                trace_id=r.trace_id, steps=r.step_count, elapsed_ms=r.elapsed_ms,
                tok_in=r.tok_in, tok_out=r.tok_out, cost_cny=r.cost_cny,
                sql_final=r.sql_final)

    # 口径命中与通过与否无关，**每道题都判**：结果对但没用口径的题恰恰是
    # 最该被看见的一类 —— 这次对是因为数据碰巧，换批数据就错。
    o.metric_used, o.metric_detail, o.metrics_injected = _metric_adherence(r, cfg)
    o.complete, o.incomplete_why = _completeness(r)

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
        if r.rejected_by == "QUOTA":
            o.reason = "配额拒绝"        # 与模型能力无关，不计入准确率分母
        else:
            o.reason = ("被护栏拦截" if (r.rejected_by or "").startswith("R-")
                        else "链路失败")
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
    if _rows_match(got, exp):
        o.passed = True
    else:
        o.reason = "结果不一致"
        o.detail = f"期望 {len(exp)} 行，实得 {len(got)} 行"
    return o


# --------------------------------------------------------------------------
# 回放
# --------------------------------------------------------------------------

def provenance_of(cfg: Config, cases: list[Case], golden: str = "") -> dict[str, Any]:
    """一份成绩的出处。

    没有这几项，结果文件事后无从分辨它跑在哪个库、哪套题上 —— 而这恰恰
    决定了这个数字能不能拿来说事。此前评测页把跑在合成样例库上的成绩
    摆在连着生产库的界面旁边，正是因为缺了这层记录。
    """
    src = (cfg.db_path.name if cfg.db_type == "duckdb"
           else _dsn_brief(cfg.dsn, cfg.upstream))
    return {
        "config": str(getattr(cfg, "path", "") or ""),
        "datasource": f"{cfg.db_type}:{src}",
        "synthetic": cfg.db_type == "duckdb",   # 样例库是 seed.py 生成的合成数据
        "org_id": cfg.default_org,
        "golden": golden or "evals/golden.jsonl",
        "n_cases": len(cases),
        "model": cfg.llm.get("model", ""),
        "tables": sorted(cfg.tables),
        "metrics": [m.name for m in cfg.metrics],
    }


def _dsn_brief(dsn: str, upstream: str = "") -> str:
    """数据源身份标识。

    经隧道时必须记 upstream 而不是本地转发端口 —— 出处这一栏存在的意义
    就是说清"这组数字出自哪个库"，记成 127.0.0.1 等于什么都没说。
    """
    kv = dict(x.split("=", 1) for x in dsn.split() if "=" in x and not x.startswith("password="))
    host = upstream or f"{kv.get('host', '?')}:{kv.get('port', '')}"
    return f"{kv.get('dbname', '?')}@{host}"


def run(cfg: Config, cases: list[Case], group: str = "current",
        verbose: bool = True, golden: str = "",
        on_progress: Callable[[int, int], None] | None = None) -> Report:
    """跑一轮回放。

    on_progress(已完成, 总数) 在**每题判完之后**回调一次，给界面报进度用 ——
    一轮盲测要几分钟，没有进度的按钮和卡死没有区别。回调里抛错不该带塌整轮，
    所以调用方自己兜住；这里不加 try，是因为回调只由本仓库内部传入。
    """
    rep = Report(group=group, n=len(cases),
                 provenance=provenance_of(cfg, cases, golden))
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
                if on_progress:
                    on_progress(i, len(cases))
                continue
            o = judge(c, r, cfg, ex)
            rep.outcomes.append(o)
            if verbose:
                mark = "✓" if o.passed else "✗"
                extra = f"  {o.reason}" if o.reason else ""
                print(f"  [{i:>2}/{len(cases)}] {mark} {c.id} {c.category:<9}"
                      f"{c.question[:26]:<28}{o.elapsed_ms:>6}ms{extra}")
            if on_progress:
                on_progress(i, len(cases))
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
    ap.add_argument("--golden", default="",
                    help="题库路径，默认 evals/golden.jsonl（样例库那份）")
    ap.add_argument("--blind", action="store_true", help="仅盲测集（验收用）")
    ap.add_argument("--all", action="store_true", help="全部题目")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题，便于冒烟")
    ap.add_argument("--out", default="", help="结果写入 JSON")
    a = ap.parse_args()

    cfg = load_cfg(a.config)
    cases = load_cases(Path(a.golden) if a.golden else None)
    if a.blind:
        cases, group = [c for c in cases if c.blind], "盲测集（最终成绩）"
    elif a.all:
        group = "全集"
    else:
        cases, group = [c for c in cases if not c.blind], "非盲测集（调参用）"
    if a.limit:
        cases = cases[: a.limit]

    print(f"回放 {group}：{len(cases)} 题  配置 {a.config}")
    rep = run(cfg, cases, group=group, golden=a.golden)
    print(summarize(rep))

    if a.out:
        Path(a.out).write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"\n结果已写入 {a.out}")


if __name__ == "__main__":
    main()
