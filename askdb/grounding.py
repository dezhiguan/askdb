"""结论里的数字接不接地 —— 纯函数，不碰 IO，不调模型。

**这一层要解决的问题**（2026-09-11 复测暴露，报告里的 BUG-A5）：

agent 已有的兜底判的是「本轮有没有成功执行过 SQL」。它挡住了"一次都没跑就编"
的 15 条，但挡不住下面这种：模型跑一条探查性的查询（`SELECT MIN(stat_date),
MAX(stat_date), COUNT(*) FROM product_stats_daily`）把那道门推开，然后照样把
答案编出来 —— 答「8 月 GMV 1,347,590.05 元、181,164 行」，而这两个数在任何
一次返回行里都不存在（真值 7,693,056,921.16）。同一形态的还有"采购金额最大的
供应商"：执行的是一条全量汇总，答案却给出一张带公司名与金额的 Top-5 表。

所以判据要从「有没有跑」换成「这个数字是不是从返回行里来的」。

**失败方向的选择。** 这层的两类错代价完全不对称：

  · 漏判（放过一个编造）—— 回到现状，不比今天更糟；
  · 误判（把一个正确答案判成编造）—— 直接毁掉一个对的回答。

所以每一处拿不准都倒向「算它接地」：只查大额数字、年份/占比一律跳过、允许由
两个返回值做一次算术得到、允许整列求和。宁可漏，不可误。

**为什么允许一次算术。** 模型经常写"全表 447,000 条，其中可售 398,082 条，
停售 48,918 条"—— 最后那个数是它自己减出来的，库里没有这一行。禁掉它等于
把这类完全正确的答案judged成编造。一次算术是实测校准出来的边界：两步以上的
推导（先减再算占比再乘）在真实答案里极少，而放开到两步会让可命中的组合爆炸，
漏判随之变多。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

#: 千分位、小数点都认；不认中文数词（"五档"这类由提示词管，不在这一层猜）
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: 低于这个绝对值的数不查。占比、评分、天数、名次、步数几乎都落在这一档，
#: 而它们恰恰是模型最常就地算的东西 —— 查它们等于制造误判。
MIN_ABS = 1000.0

#: 参与两两组合的返回值上限。真实结果集很少超过这个量，而组合数是平方级的，
#: 不封顶会让一条 200 行的结果把这层判定拖慢到秒级。
MAX_PAIRS_SOURCE = 80


def numbers_in(text: str) -> list[float]:
    """文本里的数值。千分位逗号去掉，解析不了的原样跳过。"""
    out: list[float] = []
    for m in _NUM.finditer(text or ""):
        raw = m.group(0).replace(",", "")
        if raw.endswith("."):
            raw = raw[:-1]
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    # Decimal 之类：str() 之后再试一次
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def values_of(results: Iterable[dict[str, Any]]) -> list[float]:
    """所有成功执行的返回值，外加每一列的合计。

    列合计要算进来：问"各状态各多少"时模型常把分项加起来报一个总数，
    那个总数库里没有单独一行，但它确实是从返回值来的。
    """
    vals: list[float] = []
    for r in results or []:
        rows = r.get("rows") or []
        width = 0
        for row in rows:
            cells = row if isinstance(row, (list, tuple)) else [row]
            width = max(width, len(cells))
            for c in cells:
                f = _as_float(c)
                if f is not None:
                    vals.append(f)
        for i in range(width):
            col = [_as_float(row[i]) for row in rows
                   if isinstance(row, (list, tuple)) and i < len(row)]
            col = [c for c in col if c is not None]
            if len(col) > 1:
                vals.append(sum(col))
    return vals


def _keys(x: float) -> tuple[float, float, float]:
    """一个数的三档取整。模型常把 2215.6134 写成 2215.61 或 2216。"""
    return (round(x, 4), round(x, 2), round(x, 0))


def _derivable_keys(vals: list[float]) -> set[float]:
    """返回值本身，加上任意两个之间做一次算术的结果。一次性建好供 O(1) 查。"""
    keys: set[float] = set()
    for v in vals:
        keys.update(_keys(v))
    src = vals[:MAX_PAIRS_SOURCE]
    for i, a in enumerate(src):
        for b in src[i + 1:]:
            cands = [a + b, a - b, b - a, a * b]
            if b:
                cands += [a / b, 100.0 * a / b]
            if a:
                cands += [b / a, 100.0 * b / a]
            for c in cands:
                if c != c or c in (float("inf"), float("-inf")):   # NaN / inf
                    continue
                if abs(c) > 1e18:
                    continue
                keys.update(_keys(c))
    return keys


def _year_like(x: float) -> bool:
    """1900–2100 的整数当年份跳过。它们几乎总是"2026年8月"里的那个 2026。"""
    return x == int(x) and 1900 <= x <= 2100


def ungrounded(answer: str, results: list[dict[str, Any]],
               *, min_abs: float = MIN_ABS) -> list[float]:
    """结论里追溯不到任何返回值的大额数字。空列表 = 全部接地。

    results 是本轮**每一次成功 execute_sql** 的结果（含 columns/rows），
    不是只有最后一次 —— 模型的结论经常引用更早几步的数，只看最后一次会把
    大量正确答案判成编造（实测过，误判高到不可用）。
    """
    nums = [x for x in numbers_in(answer)
            if abs(x) >= min_abs and not _year_like(x)]
    if not nums:
        return []
    vals = values_of(results)
    if not vals:
        # 一条结果都没有时不在这里判 —— 那是"没跑过"，由 agent 的 NO_EVIDENCE 管，
        # 两条判定各管各的，免得同一件事报两种原因。
        return []
    keys = _derivable_keys(vals)
    bad: list[float] = []
    for x in nums:
        if any(k in keys for k in _keys(x)):
            continue
        if x not in bad:
            bad.append(x)
    return bad


def fmt(nums: list[float]) -> str:
    """给人看的列表。整数不拖 .0。"""
    out = []
    for x in nums:
        out.append(f"{int(x):,}" if x == int(x) else f"{x:,.4f}".rstrip("0").rstrip("."))
    return "、".join(out)
