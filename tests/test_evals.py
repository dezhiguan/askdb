

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


# ---------------------------------------------------------------- 故障注入


def _chaos_cases():
    from evals.golden import Case

    return [Case(id="c1", question="有哪些文档卡在处理中超过一小时", category="single")]


def test_chaos_injection_actually_fires_and_is_judged(cfg, monkeypatch):
    """注入必须真打进链路，并按各自的判据算恢复。

    这条用例把三类故障一次跑完，钉住的是**判据**而不是某个具体数字：
    数据库超时可重试（链路应当自己回来）、模型限流不重试（askdb 现在
    确实回不来，这就是这组数字存在的意义）、Schema 漂移只要求不编造。
    """
    from evals import chaos
    from tests.test_graph import OK_SQL, FakeLlm

    rep = chaos.run(cfg, _chaos_cases(), verbose=False,
                    llm_factory=lambda: FakeLlm(OK_SQL, OK_SQL, OK_SQL))
    assert rep.n_cases == 1 and rep.skipped == 0
    got = {f.key: f for f in rep.faults}
    assert set(got) == {"db_timeout", "llm_rate_limit", "schema_drift"}
    for f in got.values():
        assert f.injected == 1
        assert all(c.fired for c in f.cases), f"{f.key} 的注入没打进去"

    # 可重试的执行超时：反思→重新生成→再执行，结果应当与基线一致
    assert got["db_timeout"].recovered == 1
    # 模型调用失败在图里直接 finalize，没有重试 —— 如实记 0，不许粉饰
    assert got["llm_rate_limit"].recovered == 0
    # 列不存在：不要求答出来，只要求别编造
    assert got["schema_drift"].recovered == 1


def test_chaos_report_reports_what_it_dropped(cfg):
    """基线跑不通的题被排除在分母外，这件事必须出现在结果里。"""
    from evals import chaos
    from evals.golden import Case

    class DeadLlm:
        def generate_sql(self, *a, **k):
            raise RuntimeError("模型不可用")

        def structured(self, schema, system, human):
            raise RuntimeError("模型不可用")

    rep = chaos.run(cfg, [Case(id="c1", question="有多少文档", category="single")],
                    verbose=False, llm_factory=DeadLlm)
    assert rep.n_cases == 0 and rep.skipped == 1
    assert all(f.injected == 0 and f.rate is None for f in rep.faults)
