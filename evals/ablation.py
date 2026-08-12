"""消融实验（技术设计说明书 §6.3）。

固定黄金集、模型、随机种子，仅改变链路配置，逐项累加：

  A  裸 Prompt        全量 schema 注入，无护栏改写、无重试、无口径
  B  A + Schema 召回  只注入命中的表
  C  B + 静态校验重试  护栏拦截后回灌真实错误重新生成
  D  C + 语义层口径    命中口径时注入定义并禁止自行构造
  E  D + 干跑阈值      单步全链路
  F  E + 多步规划      多跳能力

**F 组必须双向报告。** 多步的收益集中在 8 道多跳题上，代价却分摊在全部
58 题（成本、延迟、误用）。只报"多跳准确率提升了多少"是片面的 ——
若非多跳题的成本上升幅度超过多跳收益，该特性应回退为默认关闭的开关。

用法：
  python -m evals.ablation --groups A,B,C,D,E,F --out ablation.json
  python -m evals.ablation --groups B,F --limit 12      # 冒烟
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Callable

from askdb.config import Config, load as load_cfg

from .golden import load as load_cases
from .replay import Report, run, summarize

# 每组在基线配置上叠加的改动。刻意用"逐项累加"而非"逐项开关"，
# 这样每一行的增量就是该项的贡献，能单独归因。
GROUPS: dict[str, tuple[str, Callable[[Config], None]]] = {}


def _group(key: str, label: str):
    def deco(fn):
        GROUPS[key] = (label, fn)
        return fn
    return deco


@_group("A", "裸 Prompt（全量 schema，无护栏改写/重试/口径）")
def _a(cfg: Config) -> None:
    cfg.raw["schema_rag"]["mode"] = "all"
    cfg.raw["guard"]["max_retry"] = 0
    cfg.raw["guard"]["max_scan_rows"] = 10**9      # 相当于关掉干跑阈值
    cfg.raw["planner"]["enabled"] = False
    cfg._ablate_no_metrics = True                   # 由 run 前置处理


@_group("B", "+ Schema 召回")
def _b(cfg: Config) -> None:
    _a(cfg)
    cfg.raw["schema_rag"]["mode"] = "keyword"


@_group("C", "+ 静态校验重试")
def _c(cfg: Config) -> None:
    _b(cfg)
    cfg.raw["guard"]["max_retry"] = 2


@_group("D", "+ 语义层口径")
def _d(cfg: Config) -> None:
    _c(cfg)
    cfg._ablate_no_metrics = False


@_group("E", "+ 干跑阈值（单步全链路）")
def _e(cfg: Config) -> None:
    _d(cfg)
    cfg.raw["guard"]["max_scan_rows"] = 500_000


@_group("F", "+ 多步规划")
def _f(cfg: Config) -> None:
    _e(cfg)
    cfg.raw["planner"]["enabled"] = True


def make_cfg(base: Config, key: str) -> Config:
    cfg = copy.copy(base)
    cfg.raw = copy.deepcopy(base.raw)
    cfg.tables = dict(base.tables)
    cfg.metrics = list(base.metrics)
    cfg._ablate_no_metrics = False
    GROUPS[key][1](cfg)
    if getattr(cfg, "_ablate_no_metrics", False):
        cfg.metrics = []           # 关掉语义层：口径不再注入提示词
    return cfg


def _delta(cur: float, prev: float | None, unit: str = "pp") -> str:
    if prev is None:
        return "—"
    d = (cur - prev) * (100 if unit == "pp" else 1)
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}{unit}"


def table(reports: list[tuple[str, str, Report]]) -> str:
    head = (f"{'组':<3}{'配置':<34}{'准确率':>8}{'增量':>9}"
            f"{'误拒率':>8}{'多步误用':>9}{'平均步数':>9}{'成本':>10}{'P95':>9}")
    lines = ["", head, "-" * len(head)]
    prev = None
    for key, label, rep in reports:
        lines.append(
            f"{key:<3}{label[:32]:<34}{rep.accuracy:>7.1%}{_delta(rep.accuracy, prev):>9}"
            f"{rep.false_reject:>7.1%}{rep.multi_misuse:>8.1%}"
            f"{rep.avg_steps:>9}{('¥' + str(rep.cost)):>10}{str(rep.p95_ms) + 'ms':>9}"
        )
        prev = rep.accuracy

    # F 组双向报告：多跳收益 vs 全集代价
    by = {k: r for k, _, r in reports}
    if "E" in by and "F" in by:
        e, f = by["E"], by["F"]
        hop = lambda r: [o for o in r.outcomes if o.category == "multihop"]
        acc = lambda xs: (sum(o.passed for o in xs) / len(xs)) if xs else 0.0
        gain = acc(hop(f)) - acc(hop(e))
        cost_up = (f.cost - e.cost) / e.cost if e.cost else 0.0
        lines += [
            "",
            "F 组双向报告（多步规划）——",
            f"  多跳题准确率     {acc(hop(e)):.1%} → {acc(hop(f)):.1%}   ({gain*100:+.1f}pp)",
            f"  全集总成本       ¥{e.cost} → ¥{f.cost}   ({cost_up*100:+.1f}%)",
            f"  多步误用率       {f.multi_misuse:.1%}   本应单步却走了多步",
            f"  平均步数         {e.avg_steps} → {f.avg_steps}",
        ]
        if cost_up > 0 and gain <= 0:
            lines.append("  ⚠ 成本上升而多跳无收益 —— 该特性应回退为默认关闭的开关")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="消融实验")
    ap.add_argument("-c", "--config", default="config/askdb.yaml")
    ap.add_argument("--golden", default="",
                    help="题库路径，默认 evals/golden.jsonl（样例库那份）")
    ap.add_argument("--groups", default="A,B,C,D,E,F")
    ap.add_argument("--blind", action="store_true", help="在盲测集上跑（验收用）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    base = load_cfg(a.config)
    cases = load_cases(Path(a.golden) if a.golden else None)
    cases = [c for c in cases if c.blind] if a.blind else [c for c in cases if not c.blind]
    if a.limit:
        cases = cases[: a.limit]

    keys = [k.strip().upper() for k in a.groups.split(",") if k.strip()]
    unknown = [k for k in keys if k not in GROUPS]
    if unknown:
        raise SystemExit(f"未知分组：{unknown}，可选 {list(GROUPS)}")

    print(f"消融实验：{len(keys)} 组 × {len(cases)} 题"
          f"{'（盲测集）' if a.blind else ''}\n")
    reports: list[tuple[str, str, Report]] = []
    for k in keys:
        label = GROUPS[k][0]
        print(f"── {k} {label}")
        rep = run(make_cfg(base, k), cases, group=f"{k} {label}", verbose=False,
                  golden=a.golden)
        print(summarize(rep))
        reports.append((k, label, rep))

    print(table(reports))
    if a.out:
        Path(a.out).write_text(
            json.dumps({k: r.to_dict() for k, _, r in reports}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n结果已写入 {a.out}")


if __name__ == "__main__":
    main()
