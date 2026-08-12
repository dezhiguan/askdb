

def test_comparator_tolerates_rounding_but_not_wrong_metrics():
    """判定器必须分得清「舍入差」与「口径错」。

    实测暴露过的缺陷：Decimal 落进 str() 分支，导致
    AVG(x) 与 ROUND(AVG(x),1) 判为不等 —— 语义相同的答案被判错。
    但容差不能松到掩盖真实的口径错误（日均成本分母用行数而非天数，
    结果差 55%）。
    """
    from decimal import Decimal as D
    from evals.replay import _cell_eq, _rows_match

    # 舍入差 —— 同一个答案
    assert _cell_eq(D("233.4954287"), D("233.5"))
    # 真实路径：被判定的答案经 jsonable() 已是字符串，标准答案是 Decimal。
    # 漏掉这条，上面那行过了也没用 —— 实测就栽在这里。
    assert _cell_eq("233.4875621890547264", D("233.5"))
    assert not _cell_eq("0.16565", D("0.3727"))
    assert _cell_eq(D("0.372710"), D("0.3727"))
    assert _cell_eq(1000, 1000.00004)

    # 口径错 —— 必须判不等
    assert not _cell_eq(D("0.1656"), D("0.3727"))
    assert not _cell_eq(5, 6)
    assert not _cell_eq(D("1.0"), D("1.01"))

    # 类型不可混淆
    assert not _cell_eq(True, 1)
    assert not _cell_eq(None, 0)

    # 集合比对：行序无关，但要一一对应
    assert _rows_match([(2, "b"), (1, "a")], [(1, "a"), (2, "b")])
    assert not _rows_match([(1, "a")], [(1, "a"), (2, "b")])
    assert not _rows_match([(1, "a"), (1, "a")], [(1, "a"), (2, "b")])
