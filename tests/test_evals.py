

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
    from tests.cases.test_e_agent import OK_SQL, FakeLlm, _act

    def _llm():
        """查一次 → （被注入打掉时）再查一次 → 收尾。

        第二步不是凑数：**"可重试的超时"这条判据要求链路自己回来**。只给一步
        的话，注入打掉第一次之后 agent 直接收尾，结果与基线不同，会被判成
        "沉默的错误"——那是替身不够，不是链路不恢复。
        """
        return FakeLlm(_act(sql=OK_SQL), _act(sql=OK_SQL),
                       _act(finish=True, answer="有结果"))

    rep = chaos.run(cfg, _chaos_cases(), verbose=False, llm_factory=_llm)
    assert rep.n_cases == 1 and rep.skipped == 0
    got = {f.key: f for f in rep.faults}
    assert set(got) == {"db_timeout", "llm_rate_limit", "schema_drift"}
    for f in got.values():
        assert f.injected == 1
        assert all(c.fired for c in f.cases), f"{f.key} 的注入没打进去"

    # 可重试的执行超时：反思→重新生成→再执行，结果应当与基线一致
    assert got["db_timeout"].recovered == 1
    # 模型调用失败在图里直接收尾，没有重试 —— 如实记 0，不许粉饰
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


# ---------------------------------------------------------------- 口径命中


def test_metric_adherence_is_deterministic_and_scoped():
    """业务口径命中率必须是**确定性判定**，且只在判得动的题上算。

    口径存在的意义就是「按定义算，别凭直觉算」：expr 是认证定义式，
    naive 是与之对照的直觉写法。这里钉住三件事 ——
      · 没注入口径、或没生成 SQL 的题不进分母（返回 None）。
        混进来会让"命中率"被题目构成推着走，测不出任何东西。
      · 命中取"注入的口径里任意一条 expr 出现在 SQL 里"：召回一次最多塞
        max_metrics 条，其中常有一条与本题无关，要求全部出现会误判。
      · 用了 naive 写法必须在说明里点名 —— 那正是口径要防的那件事。
    """
    from types import SimpleNamespace as NS

    from askdb.config import Metric
    from evals.replay import _metric_adherence

    jd = Metric(name="JD 文档数", aliases=[], scope=["documents"],
                expr="COUNT(*) FILTER (WHERE chunk_type = 'JD')", naive="COUNT(*)")
    docs = Metric(name="文档数", aliases=[], scope=["documents"],
                  expr="COUNT(*) FILTER (WHERE parse_status = 'COMPLETED')",
                  naive="COUNT(*)")
    cfg = NS(metrics=[jd, docs])

    def r(sql: str, hit: list[str]):
        return NS(sql_final=sql, metrics_hit=hit)

    # 用上了定义式 —— 换行与大小写不该影响判定
    ok, detail, graded = _metric_adherence(
        r("select count(*)\n  filter (where CHUNK_TYPE = 'JD')\nfrom documents",
          ["JD 文档数"]), cfg)
    assert ok is True and detail == "" and graded == ["JD 文档数"]

    # 注入两条只用上一条 —— 仍算命中，另一条与本题无关
    assert _metric_adherence(
        r("select count(*) filter (where chunk_type = 'JD') from documents",
          ["JD 文档数", "文档数"]), cfg)[0] is True

    # 凭直觉写 COUNT(*) —— 未命中，且必须点名说它改用了直觉写法
    ok, detail, _ = _metric_adherence(r("select count(*) from documents", ["JD 文档数"]), cfg)
    assert ok is False
    assert "JD 文档数" in detail and "直觉写法" in detail

    # 判不动的两种情形都必须是 None，不是 False —— False 会把它算成失分
    assert _metric_adherence(r("select count(*) from documents", []), cfg)[0] is None
    assert _metric_adherence(r("", ["JD 文档数"]), cfg)[0] is None
    # 口径没写 expr 时同样判不动
    bare = NS(metrics=[Metric(name="慢检索", aliases=[], scope=["retrieval_logs"])])
    assert _metric_adherence(r("select 1", ["慢检索"]), bare)[0] is None


def test_completeness_counts_only_answers_that_came_back():
    """结果完整度只在**真的跑出结果集**的题上算，且三种不完整都要认出来。

    没跑出结果的题（被拒、链路失败）进分母，等于拿"没跑出来"去压"跑出来但
    不完整" —— 那两件事已经分别由拦截率和准确率在报了，混在一起两个数都废。

    三个信号都是链路自己记下的事实标记，不是判断：截断、提前收敛、召回盲选。
    """
    from types import SimpleNamespace as NS

    from evals.replay import _completeness

    def r(**kw):
        base = dict(ok=True, truncated=False, converged_early="",
                    recall_blind=False, recall_note="")
        return NS(**{**base, **kw})

    assert _completeness(r()) == (True, "")
    assert _completeness(r(truncated=True))[0] is False
    assert "截断" in _completeness(r(truncated=True))[1]
    assert _completeness(r(converged_early="已达步数上限（3 步）"))[0] is False
    assert _completeness(r(recall_blind=True, recall_note="白名单 32 张全给了"))[0] is False
    # 多种同时命中要一并说清，不能只报第一条
    ok, why = _completeness(r(truncated=True, recall_blind=True))
    assert ok is False and "截断" in why and "盲选" in why
    # 没跑出结果 → 不进分母，必须是 None 而不是 False
    assert _completeness(r(ok=False))[0] is None
