"""数字接地校验（askdb/grounding.py，BUG-A5）。

**这一层判的是"结论里的数字是不是从返回行里来的"**，不是"有没有跑过 SQL"。

换判据的原因在复测里：模型跑一条探查性查询（`SELECT MIN(stat_date),
MAX(stat_date), COUNT(*) …`）把"跑过了"那道门推开，然后照样把答案编出来 ——
答「8 月 GMV 1,347,590.05 元」，而真值是 7,693,056,921.16，那两个数在任何一次
返回行里都不存在。原有兜底对这一形态完全无感。

**用例的取向跟着模块的取向走：宁可漏判，不可误判。** 这一层的两类错代价不对称
—— 漏判只是回到现状，误判会直接毁掉一个正确回答。所以下面大部分用例钉的是
"这些情形必须判为接地"，而不是"这些必须抓出来"；抓得准只有一条主用例。
"""

from __future__ import annotations

from askdb import grounding


def _res(rows, columns=None):
    return {"columns": columns or ["v"], "rows": rows}


# ---------------------------------------------------------------- 数字抽取

def test_numbers_are_parsed_with_thousands_separators():
    """千分位要认 —— 模型写出来的大数几乎都是带逗号的。"""
    assert grounding.numbers_in("共 1,347,590.05 元，181,164 行") == [1347590.05, 181164.0]
    assert grounding.numbers_in("") == []
    assert grounding.numbers_in("没有数字") == []


def test_a_trailing_period_is_not_part_of_the_number():
    """句号不能被吃进小数点：「共 4000.」里那个数是 4000，不是解析失败。"""
    assert grounding.numbers_in("共 4000.") == [4000.0]


# ---------------------------------------------------------------- 取值归集

def test_values_include_column_totals():
    """列合计要算进来。

    问"各状态各多少"时模型常把分项加起来报一个总数，那个总数库里没有单独一行，
    但它确实是从返回值来的 —— 不算进来就会把这类正确答案判成编造。
    """
    vals = grounding.values_of([_res([[3000], [4000], [5000]])])
    assert 12000.0 in vals, "三行的合计没被算进来"


def test_single_row_has_no_column_total():
    """只有一行时不造合计 —— 那个"合计"就是它自己，白白多一个候选值
    只会放大漏判。"""
    vals = grounding.values_of([_res([[3000]])])
    assert vals == [3000.0]


def test_values_survive_messy_cells():
    """真实结果里混着字符串数字、None、布尔和 Decimal。

    布尔**必须**排除：Python 里 True == 1，放进来等于凭空多一个 1 的候选值。
    """
    from decimal import Decimal

    vals = grounding.values_of([_res([["1,200", None, True, Decimal("3.5"), "abc"]])])
    assert 1200.0 in vals and 3.5 in vals
    assert 1.0 not in vals, "布尔被当成数字了"


def test_scalar_rows_are_accepted():
    """有的执行结果每行就是一个标量而不是列表 —— 不能因此整条丢掉。"""
    assert grounding.values_of([_res([1500, 2500])]) == [1500.0, 2500.0]


def test_empty_and_missing_results_yield_nothing():
    assert grounding.values_of([]) == []
    assert grounding.values_of([{"columns": ["v"]}]) == []


# ---------------------------------------------------------------- 主判定

def test_a_fabricated_number_is_caught():
    """BUG-A5 的原形：跑的是探查查询，答的是一个不存在的 GMV。"""
    probe = _res([["2026-08-01", "2026-08-31", 181164]],
                 ["min", "max", "cnt"])
    bad = grounding.ungrounded("8 月 GMV 为 1,347,590.05 元，共 181,164 行", [probe])
    assert bad == [1347590.05]
    assert grounding.fmt(bad) == "1,347,590.05"


def test_a_value_straight_from_the_rows_is_grounded():
    assert grounding.ungrounded("共 181,164 行", [_res([[181164]])]) == []


def test_one_arithmetic_step_is_allowed():
    """模型常写"全表 447,000，可售 398,082，停售 48,918"—— 最后那个是它自己
    减出来的，库里没有这一行。禁掉它等于把完全正确的答案判成编造。"""
    res = _res([[447000, 398082]], ["total", "on_sale"])
    assert grounding.ungrounded("全表 447,000 条，可售 398,082 条，停售 48,918 条",
                                [res]) == []


def test_numbers_below_the_threshold_are_skipped():
    """占比、评分、天数、名次几乎都在 1000 以下，而它们恰恰是模型最常就地算的
    东西 —— 查它们等于制造误判。"""
    assert grounding.ungrounded("占比 37.5%，排名第 3", [_res([[999999]])]) == []


def test_years_are_never_treated_as_fabricated():
    """1900–2100 的整数当年份跳过：它们几乎总是"2026 年 8 月"里的那个 2026。"""
    assert grounding.ungrounded("2026 年的数据", [_res([[181164]])]) == []
    assert grounding._year_like(2026.0) is True
    assert grounding._year_like(2026.5) is False
    assert grounding._year_like(181164.0) is False


def test_no_results_means_this_layer_stays_quiet():
    """一条结果都没有时不在这里判 —— 那是"没跑过"，由 agent 的 NO_EVIDENCE 管。
    两条判定各管各的，免得同一件事报出两种原因。"""
    assert grounding.ungrounded("答案是 1,347,590", []) == []
    assert grounding.ungrounded("答案是 1,347,590", [_res([])]) == []


def test_an_answer_without_big_numbers_short_circuits():
    assert grounding.ungrounded("没有查到相关数据", [_res([[181164]])]) == []


def test_each_fabricated_number_is_reported_once():
    """同一个数在结论里出现两次只报一次 —— 提示语里重复一遍没有信息量。"""
    bad = grounding.ungrounded("共 5,000,000 元；其中 5,000,000 元已结算",
                               [_res([[1]])])
    assert bad == [5000000.0]


def test_division_by_zero_does_not_blow_up():
    """返回值里有 0 是常态（COUNT 为 0、金额为 0）。组合时除零必须被跳过，
    而不是抛出来把整条链路带崩 —— 这一层是旁路校验，不该有能力让查询失败。"""
    bad = grounding.ungrounded("合计 9,999,999", [_res([[0], [0], [1234]])])
    assert bad == [9999999.0]


def test_threshold_is_adjustable():
    """min_abs 可调：影子期要看不同档位的误判率，写死就没法比。"""
    assert grounding.ungrounded("共 500 件", [_res([[1]])]) == []
    assert grounding.ungrounded("共 500 件", [_res([[1]])], min_abs=100) == [500.0]


def test_fmt_keeps_integers_clean():
    """整数不拖 .0 —— 这句话是给人看的，不是日志。"""
    assert grounding.fmt([181164.0]) == "181,164"
    assert grounding.fmt([1347590.05]) == "1,347,590.05"
    assert grounding.fmt([]) == ""
