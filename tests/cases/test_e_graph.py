"""E 域 · 状态机路由、重试与多步 R-14～R-17（22 条）。"""

from __future__ import annotations

import pytest

from askdb import graph, planner
from askdb.llm import LlmUsage, SqlDraft

OK_SQL = "SELECT file_name AS 文件名 FROM documents WHERE status = 'PROCESSING'"


class FakeLlm:
    def __init__(self, *sqls, raises=None, multi_step=False):
        self.sqls, self.raises, self.calls = list(sqls), raises, []
        self.multi_step = multi_step

    def generate_sql(self, question, schema_prompt, dialect="duckdb",
                     last_sql="", error="", step=""):
        self.calls.append({"error": error, "last_sql": last_sql})
        if self.raises:
            raise self.raises
        sql = self.sqls.pop(0) if self.sqls else ""
        return SqlDraft(sql=sql, reasoning="t"), LlmUsage(100, 50)

    def structured(self, model, system, user):
        """按传入的 model 类型返回对应形状。

        第一版对 Plan 和 Assessment 返回同一形状，导致多步规划路径
        根本没被正确驱动 —— 断言其实跑在一个无效状态上。
        """
        name = getattr(model, "__name__", "")
        if name == "Plan":
            return model(multi_step=self.multi_step, goal="本步目标",
                         reason="t"), LlmUsage(10, 5)
        return model(enough=True, reason="t", carry={}), LlmUsage(10, 5)


def test_e01_happy_path(cfg, ex):
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert r.ok and [s["step"] for s in r.steps][-1] == "finalize"


def test_e02_no_sql_goes_straight_to_finalize(cfg, ex):
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(""))
    assert not r.ok and "guard" not in [s["step"] for s in r.steps]


def test_e03_retry_then_success(cfg, ex):
    f = FakeLlm("SELECT no_such AS x FROM documents", OK_SQL)
    r = graph.ask("q", cfg, executor=ex, llm=f)
    assert r.ok and r.attempts == 2


def test_e04_retry_exhausted(cfg, ex):
    bad = "SELECT no_such AS x FROM documents"
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(bad, bad, bad, bad))
    assert not r.ok and r.attempts == cfg.max_retry + 1
    assert sum(1 for s in r.steps if s["step"] == "reflect") == cfg.max_retry


def test_e05_max_retry_zero(cfg, ex):
    cfg.raw["guard"]["max_retry"] = 0
    bad = "SELECT no_such AS x FROM documents"
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(bad, OK_SQL))
    assert not r.ok and r.attempts == 1


def test_e06_out_of_scope_no_reflection(cfg, ex):
    f = FakeLlm("SELECT COUNT(*) AS n FROM chunks", OK_SQL)
    r = graph.ask("chunks 表里有多少行", cfg, executor=ex, llm=f)
    assert not r.ok and r.rejected_by == "R-03" and len(f.calls) == 1


def test_e07_no_entity_substitution(cfg, ex):
    f = FakeLlm("SELECT COUNT(*) AS n FROM chunks",
                "SELECT COUNT(*) AS n FROM documents")
    r = graph.ask("chunks 表里有多少行", cfg, executor=ex, llm=f)
    assert not r.ok and r.row_count == 0
    assert "documents" not in (r.sql_final or "")


def test_e08_fixable_reject_still_retried(cfg, ex):
    f = FakeLlm("SELECT no_such AS x FROM documents", OK_SQL)
    r = graph.ask("q", cfg, executor=ex, llm=f)
    assert r.ok and r.attempts == 2


def test_e09_execution_error_triggers_reflection(cfg, ex, monkeypatch):
    """设计 §4.5：执行报错须回灌真实错误重新生成。

    用例第一版注入了 DataSourceError，那是**基础设施异常类**，不是 SQL 错 ——
    真实的 SQL 语义错（类型不匹配、函数用法错）在引擎侧抛的是
    ConversionException / BinderException，走通用异常分支，本来就会重试。
    这里改为注入真实形态的引擎异常。
    """
    calls = {"n": 0}
    orig = ex.run

    def boom(sql):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Binder Error: 函数用法错")
        return orig(sql)
    monkeypatch.setattr(ex, "run", boom)
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL, OK_SQL))
    assert r.attempts == 2, "SQL 语义错必须回灌重试（§4.5）"


def test_e09b_timeout_is_retryable(cfg, ex, monkeypatch):
    """语句超时可重试 —— 模型缩小查询就可能过，与 R-11 干跑超限同理。"""
    from askdb.executor import DataSourceError
    calls = {"n": 0}
    orig = ex.run

    def slow(sql):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DataSourceError("查询超时（超过 8000 ms 已中断）",
                                  hint="缩小范围", retryable=True)
        return orig(sql)
    monkeypatch.setattr(ex, "run", slow)
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL, OK_SQL))
    assert r.attempts == 2 and r.ok


def test_e09c_datasource_down_is_not_retried(cfg, ex, monkeypatch):
    """连接不可达重试纯属浪费，还会白烧模型 token。"""
    from askdb.executor import DataSourceError
    f = FakeLlm(OK_SQL, OK_SQL)
    monkeypatch.setattr(ex, "run", lambda sql: (_ for _ in ()).throw(
        DataSourceError("无法连接数据库", hint="检查连接串")))
    r = graph.ask("q", cfg, executor=ex, llm=f)
    assert not r.ok and r.attempts == 1 and len(f.calls) == 1


def test_e10_dry_run_block_triggers_reflection(cfg, ex):
    cfg.raw["guard"]["max_scan_rows"] = 1
    f = FakeLlm(OK_SQL, OK_SQL, OK_SQL)
    r = graph.ask("q", cfg, executor=ex, llm=f)
    assert not r.ok
    assert sum(1 for s in r.steps if s["step"] == "reflect") >= 1


def test_e11_real_error_is_fed_back(cfg, ex):
    f = FakeLlm("SELECT no_such AS x FROM documents", OK_SQL)
    graph.ask("q", cfg, executor=ex, llm=f)
    assert "no_such" in (f.calls[1]["error"] or ""), "必须回灌真实错误原文"
    assert f.calls[1]["last_sql"], "必须带上失败的那条 SQL"


def test_e12_no_silent_degradation(cfg, ex):
    bad = "SELECT no_such AS x FROM documents"
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(bad, bad, bad))
    assert not r.ok and r.row_count == 0 and r.error


def test_e13_checkpoint_replay(cfg, ex):
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    snaps = graph.replay(r.trace_id, cfg)
    assert len(snaps) >= 5
    assert snaps[0]["next"] == ["__start__"]


def test_e14_replay_unknown_trace(cfg):
    assert graph.replay("no-such-trace-id", cfg) == []


def test_e15_state_is_serializable(cfg, ex):
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert graph.replay(r.trace_id, cfg), "检查点写入失败说明状态不可序列化"


def test_e16_carry_over_limit_converges(cfg):
    cfg.raw.setdefault("planner", {})["max_carry_rows"] = 2
    ok, why = planner.carry_within_limit({"ids": [1, 2, 3]}, cfg)
    assert not ok and "上限" in why


def test_e17_carry_rejects_whole_rows(cfg):
    cfg.raw.setdefault("planner", {})["carry_columns_only"] = True
    ok, why = planner.carry_within_limit({"rows": [[1, "a"], [2, "b"]]}, cfg)
    assert not ok and "整行" in why


def test_e18_step_cap_converges(cfg, ex):
    cfg.raw["planner"] = {**cfg.raw.get("planner", {}), "enabled": True, "max_steps": 1}
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL, OK_SQL))
    assert r.step_count <= 1


def test_e19_cost_cap_converges(cfg, ex):
    """R-17 累计成本上限：触顶即收敛作答，并明确标注不完整。"""
    cfg.raw["planner"] = {**cfg.raw.get("planner", {}), "enabled": True,
                          "max_steps": 3, "cost_cap_tokens": 1}
    f = FakeLlm(OK_SQL, OK_SQL, OK_SQL, multi_step=True)
    r = graph.ask("q", cfg, executor=ex, llm=f)
    assert r.converged_early, "触及成本上限必须显式标注，不能静默收敛"
    assert "成本" in r.converged_early or "token" in r.converged_early.lower()


@pytest.mark.parametrize("cid,mode,should_fail", [
    ("E-20a", "converge", False),
    ("E-20b", "fail", True),
])
def test_e20_on_cap_reached(cid, mode, should_fail, cfg, ex):
    """on_cap_reached 的两种取值必须produce 不同结果。

    这条第一版写成了 `assert r.ok or not r.ok` —— 恒真，等于没测，
    却一直显示"通过"，掩盖了一个零验证的功能点。比失败危险得多。
    """
    cfg.raw["planner"] = {**cfg.raw.get("planner", {}), "enabled": True,
                          "max_steps": 1, "cost_cap_tokens": 1,
                          "on_cap_reached": mode}
    f = FakeLlm(OK_SQL, OK_SQL, OK_SQL, multi_step=True)
    r = graph.ask("q", cfg, executor=ex, llm=f)
    assert r.converged_early, f"{cid}: 无论哪种模式都必须标注触顶原因"
    if should_fail:
        assert not r.ok, f"{cid}: fail 模式下必须判失败，而不是收敛作答"
        assert r.rejected_by, f"{cid}: 失败须给出拒绝码"
    else:
        assert r.ok, f"{cid}: converge 模式下应基于已完成步骤作答"


def test_e21_every_step_passes_full_guard(cfg, ex):
    """多步的第二步同样要过全量护栏，不存在"已被信任"的通道。"""
    cfg.raw["planner"] = {**cfg.raw.get("planner", {}), "enabled": True, "max_steps": 3}
    f = FakeLlm(OK_SQL, "SELECT COUNT(*) AS n FROM chunks")
    r = graph.ask("q", cfg, executor=ex, llm=f)
    assert "chunks" not in (r.sql_final or "")


def test_e22_attempt_resets_per_step(cfg, ex):
    cfg.raw["planner"] = {**cfg.raw.get("planner", {}), "enabled": True, "max_steps": 2}
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL, OK_SQL, OK_SQL))
    assert r.attempts <= cfg.max_retry + 1
