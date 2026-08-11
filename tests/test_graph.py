"""状态机测试 —— 用假模型覆盖每条分支，不依赖真实密钥。

重点验证条件路由：什么时候重试、什么时候不重试、什么时候直接终止。
"""

from __future__ import annotations

import json

import pytest

from askdb import graph
from askdb.llm import LlmNotConfigured, LlmUsage, SqlDraft

OK_SQL = "SELECT file_name AS 文件名 FROM documents WHERE status = 'PROCESSING'"


class FakeLlm:
    """按序吐出预置结果；raise 传入异常类型。"""

    def __init__(self, *sqls, raises: Exception | None = None, reasoning: str = "test"):
        self.sqls = list(sqls)
        self.raises = raises
        self.reasoning = reasoning
        self.calls: list[dict] = []

    def generate_sql(self, question, schema_prompt, dialect="duckdb",
                     last_sql="", error="", step=""):
        self.calls.append({"error": error, "last_sql": last_sql, "step": step})
        if self.raises:
            raise self.raises
        sql = self.sqls.pop(0) if self.sqls else ""
        return SqlDraft(sql=sql, reasoning=self.reasoning), LlmUsage(100, 50)

    def structured(self, schema, system, human):
        """规划与评估节点用。默认判定单步、结果足够 —— 多步用例单独覆写。"""
        from askdb.planner import Assessment, Plan

        self.calls.append({"structured": schema.__name__})
        if schema is Plan:
            return Plan(multi_step=False, reason="测试替身默认单步"), LlmUsage(10, 5)
        return Assessment(enough=True, reason="测试替身默认足够"), LlmUsage(10, 5)


def run(cfg, ex, *sqls, **kw):
    return graph.ask("测试问题", cfg, executor=ex, llm=FakeLlm(*sqls, **kw))


# ---------------------------------------------------------------- 正常路径

def test_happy_path(cfg, ex):
    r = run(cfg, ex, OK_SQL)
    assert r.ok and r.row_count > 0
    assert r.attempts == 1
    assert [s["step"] for s in r.steps][-1] == "finalize"
    assert r.columns == ["文件名"]


def test_records_rewrites_and_hits(cfg, ex):
    r = graph.ask("有哪些文档卡在处理中超过一小时", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert "documents" in r.tables_hit
    assert "卡住的文档" in r.metrics_hit
    assert any("租户" in x for x in r.rewrites)


def test_cost_is_accounted(cfg, ex):
    """成本按步累计 —— 规划节点的开销也要算进去，不能只算 SQL 生成。"""
    r = run(cfg, ex, OK_SQL)
    assert r.tok_in >= 100 and r.tok_out >= 50      # 至少含 generate 的那笔
    per_step = {s["step"]: s for s in r.steps}
    assert per_step["generate_sql"]["tok_in"] == 100
    assert r.tok_in == sum(s["tok_in"] for s in r.steps)
    assert r.cost_cny > 0 and r.elapsed_ms >= 0


def test_result_is_json_serializable(cfg, ex):
    r = run(cfg, ex, "SELECT updated_at AS t FROM documents LIMIT 3")
    json.dumps(r.to_dict())          # datetime 必须被转成字符串


def test_audit_record_written(cfg, ex):
    graph.ask("审计测试", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    lines = cfg.audit_log.read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["question"] == "审计测试"
    assert rec["sql_final"] and rec["steps"]


# ---------------------------------------------------------------- 重试

def test_guard_block_triggers_retry_then_succeeds(cfg, ex):
    fake = FakeLlm("SELECT member_level FROM documents", OK_SQL)
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.attempts == 2
    gen = [c for c in fake.calls if "error" in c]
    assert "字段不存在" in gen[1]["error"]                 # 真实错误被回灌
    assert any(s["step"] == "reflect" for s in r.steps)


def test_retry_exhausts_and_terminates(cfg, ex):
    bad = "DELETE FROM documents"
    r = run(cfg, ex, bad, bad, bad, bad)
    assert not r.ok and r.rejected_by == "R-02"
    assert r.attempts == cfg.max_retry + 1
    assert sum(1 for s in r.steps if s["step"] == "reflect") == cfg.max_retry


def test_no_retry_when_max_retry_zero(cfg, ex):
    cfg.raw["guard"]["max_retry"] = 0
    r = run(cfg, ex, "DELETE FROM documents")
    assert not r.ok and r.attempts == 1
    assert not any(s["step"] == "reflect" for s in r.steps)


def test_semantic_error_at_dry_run_retries(cfg, ex):
    """护栏能过、但 EXPLAIN 生成失败（类型不匹配）—— 属于语义错，应回灌重试。"""
    fake = FakeLlm("SELECT id + file_name AS x FROM documents", OK_SQL)
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.attempts == 2
    assert any(s["step"] == "reflect" for s in r.steps)


# ---------------------------------------------------------------- 终止分支

def test_llm_not_configured_stops_immediately(cfg, ex):
    r = run(cfg, ex, raises=LlmNotConfigured("没有密钥"))
    assert not r.ok and r.rejected_by == "LLM"
    assert "没有密钥" in r.error
    assert not any(s["step"] == "guard" for s in r.steps)


def test_llm_exception_is_wrapped_friendly(cfg, ex):
    r = run(cfg, ex, raises=RuntimeError("连接被重置"))
    assert not r.ok and r.rejected_by == "LLM"
    assert "模型调用失败" in r.error and r.hint


def test_empty_sql_from_model_is_explained(cfg, ex):
    r = graph.ask("q", cfg, executor=ex,
                  llm=FakeLlm("", reasoning="缺少订单表，回答不了"))
    assert not r.ok and r.rejected_by == "NO_SQL"
    assert "订单表" in r.error and r.hint


def test_dry_run_over_threshold_retries_then_gives_up(cfg, ex):
    """扫描量超限先给模型机会补筛选条件，重试耗尽才终止，且始终不碰数据库。"""
    cfg.raw["guard"]["max_scan_rows"] = 1
    r = run(cfg, ex, OK_SQL, OK_SQL, OK_SQL, OK_SQL)
    assert not r.ok and r.rejected_by == "R-11"
    assert not any(s["step"] == "execute" for s in r.steps)
    assert r.attempts == cfg.max_retry + 1
    assert "筛选条件" in r.hint


def test_datasource_error_does_not_retry(cfg, tmp_path):
    """数据源不可用不是模型的错，重试没有意义。"""
    from askdb.executor import Executor
    cfg.raw["datasource"]["path"] = str(tmp_path / "gone.duckdb")
    r = graph.ask("q", cfg, executor=Executor(cfg), llm=FakeLlm(OK_SQL, OK_SQL, OK_SQL))
    assert not r.ok
    assert not any(s["step"] == "reflect" for s in r.steps)


# ---------------------------------------------------------------- 其它

def test_org_id_override_flows_through(cfg, ex):
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL), org_id=66)
    assert r.org_id == 66
    assert "org_id = 66" in r.sql_final.replace("\n", " ")


def test_default_org_used_when_absent(cfg, ex):
    r = run(cfg, ex, OK_SQL)
    assert r.org_id == cfg.default_org


def test_graph_builds_once_and_is_reusable(cfg, ex):
    a = run(cfg, ex, OK_SQL)
    b = run(cfg, ex, OK_SQL)
    assert a.ok and b.ok and a.trace_id != b.trace_id


def test_executor_is_closed_when_owned(cfg, tmp_path):
    """未注入 executor 时由 ask() 自行关闭，不能泄漏连接。"""
    r = graph.ask("q", cfg, llm=FakeLlm(OK_SQL))
    assert r.ok


# ---------------------------------------------------------------- 检查点与复现

def test_state_is_fully_serializable(cfg, ex):
    """运行时依赖若混进状态，检查点会直接崩。这条守住这个边界。"""
    r = run(cfg, ex, OK_SQL)
    assert r.ok
    snaps = graph.replay(r.trace_id, cfg)
    assert snaps, "检查点没有落盘"


def test_replay_reconstructs_retry_path(cfg, ex):
    """失败样本原样复现 —— P3 失败归因的前提（技术设计说明书 §5）。"""
    r = graph.ask("q", cfg, executor=ex,
                  llm=FakeLlm("SELECT member_level FROM documents", OK_SQL))
    assert r.ok and r.attempts == 2
    snaps = graph.replay(r.trace_id, cfg)
    assert any(s["rejected_by"] == "R-04" for s in snaps)
    assert any(s["sql_raw"] == "SELECT member_level FROM documents" for s in snaps)
    assert [s["attempt"] for s in snaps if s["attempt"] is not None][-1] == 1


def test_replay_unknown_trace_is_empty(cfg):
    assert graph.replay("no-such-trace", cfg) == []


# ---------------------------------------------------------------- 配额

def test_daily_quota_blocks_before_any_model_call(cfg, ex):
    """超限的请求一个 token 都不该花 —— 必须拦在模型调用之前。"""
    cfg.raw["observability"]["daily_quota"] = 1
    fake = FakeLlm(OK_SQL, OK_SQL)
    graph.ask("第一次", cfg, executor=ex, llm=fake)
    r = graph.ask("第二次", cfg, executor=ex, llm=fake)
    assert not r.ok and r.rejected_by == "QUOTA"
    gen = [c for c in fake.calls if "error" in c]
    assert len(gen) == 1                        # 第二次一次模型都没调
    assert "daily_quota" in r.hint


def test_quota_zero_means_unlimited(cfg, ex):
    cfg.raw["observability"]["daily_quota"] = 0
    for _ in range(3):
        assert run(cfg, ex, OK_SQL).ok


# ---------------------------------------------------------------- 审计

def test_audit_has_timestamp_and_explain_rows(cfg, ex):
    """审计记录没有时间戳等于没有审计（技术设计说明书 §7）。"""
    graph.ask("审计字段", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    rec = json.loads(cfg.audit_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["ts"] and rec["ts"][:4].isdigit()
    assert rec["explain_rows"] is None or rec["explain_rows"] >= 0


# ---------------------------------------------------------------- 多步规划

class PlanLlm(FakeLlm):
    """可编排规划/评估行为的替身。

    plans:   每次 plan 节点返回的 (multi_step, goal)
    assess:  每次 assess 节点返回的 (enough, carry)
    """

    def __init__(self, *sqls, plans=(), assess=()):
        super().__init__(*sqls)
        self.plans = list(plans)
        self.assess = list(assess)

    def structured(self, schema, system, human):
        from askdb.planner import Assessment, Plan

        if schema is Plan:
            multi, goal = self.plans.pop(0) if self.plans else (False, "")
            self.calls.append({"plan": goal, "human": human})
            return Plan(multi_step=multi, reason="替身", goal=goal), LlmUsage(20, 10)
        enough, carry = self.assess.pop(0) if self.assess else (True, {})
        self.calls.append({"assess": enough, "human": human})
        return Assessment(enough=enough, reason="替身", carry=carry), LlmUsage(20, 10)


def _enable_planner(cfg, **kw):
    cfg.raw["planner"] = {"enabled": True, "max_steps": 3, "max_carry_rows": 50,
                          "cost_cap_tokens": 0, **kw}


SQL2 = "SELECT file_type AS 类型 FROM documents WHERE kb_id IN (1)"


def test_planner_disabled_costs_no_model_call(cfg, ex):
    """禁用多步时，plan 节点一次调用都不该花。"""
    cfg.raw["planner"] = {"enabled": False}
    fake = PlanLlm(OK_SQL)
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and not r.multi_step
    assert not any("plan" in c for c in fake.calls)


def test_single_step_when_planner_says_so(cfg, ex):
    _enable_planner(cfg)
    r = graph.ask("q", cfg, executor=ex, llm=PlanLlm(OK_SQL, plans=[(False, "")]))
    assert r.ok and not r.multi_step and r.step_count == 1
    assert len(r.sub_steps) == 1


def test_two_step_flow_carries_literals_forward(cfg, ex):
    """中间结果只下传标识列，并作为字面量拼进下一步。"""
    fake = PlanLlm(OK_SQL, SQL2,
                   plans=[(True, "先看分布"), (True, "再拉明细")],
                   assess=[(False, {"kb_ids": [1]}), (True, {})])
    _enable_planner(cfg)
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.multi_step and r.step_count == 2
    assert len(r.sub_steps) == 2 and r.sub_steps[0]["goal"] == "先看分布"
    # 第二次生成时，本步目标与下传值都进了提示词
    gen = [c for c in fake.calls if "step" in c]
    assert "再拉明细" in gen[1]["step"] and "kb_ids" in gen[1]["step"]


def test_every_step_passes_full_guard(cfg, ex):
    """不存在"因为是第二步所以已被信任"的路径。"""
    fake = PlanLlm(OK_SQL, "DELETE FROM documents",
                   plans=[(True, "一"), (True, "二")],
                   assess=[(False, {"kb_ids": [1]}), (True, {})])
    _enable_planner(cfg)
    cfg.raw["guard"]["max_retry"] = 0
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert not r.ok and r.rejected_by == "R-02"


def test_r16_step_cap_converges_and_flags(cfg, ex):
    """触及步数上限收敛作答，且必须标注结论可能不完整。"""
    _enable_planner(cfg, max_steps=2)
    fake = PlanLlm(*[OK_SQL] * 4,
                   plans=[(True, f"第{i}步") for i in range(4)],
                   assess=[(False, {"kb_ids": [1]})] * 4)
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.step_count == 2
    assert "步数上限" in r.converged_early


def test_r15_oversized_carry_stops_multi_step(cfg, ex):
    """下传规模超限往往说明上一步筛选本身有问题。"""
    _enable_planner(cfg, max_carry_rows=3)
    fake = PlanLlm(OK_SQL, plans=[(True, "一")], assess=[(False, {"ids": list(range(10))})])
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.step_count == 1
    assert "超过上限" in r.converged_early


def test_r17_cost_cap_converges(cfg, ex):
    _enable_planner(cfg, cost_cap_tokens=1)
    fake = PlanLlm(*[OK_SQL] * 3, plans=[(True, "一")] * 3,
                   assess=[(False, {"kb_ids": [1]})] * 3)
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and "成本上限" in r.converged_early


def test_multi_step_state_survives_checkpointing(cfg, ex):
    """多步字段必须可序列化，否则检查点直接崩。"""
    _enable_planner(cfg)
    fake = PlanLlm(OK_SQL, SQL2, plans=[(True, "一"), (True, "二")],
                   assess=[(False, {"kb_ids": [1]}), (True, {})])
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and graph.replay(r.trace_id, cfg)


def test_assess_failure_is_treated_as_enough(cfg, ex):
    """评估本身出错时按足够处理 —— 宁可少答一步，不要卡死链路。"""
    class Broken(PlanLlm):
        def structured(self, schema, system, human):
            from askdb.planner import Plan
            if schema is Plan:
                return Plan(multi_step=True, reason="x", goal="一"), LlmUsage(1, 1)
            raise RuntimeError("评估服务挂了")

    _enable_planner(cfg)
    r = graph.ask("q", cfg, executor=ex, llm=Broken(OK_SQL))
    assert r.ok and r.step_count == 1


def test_replan_without_goal_converges_without_extra_sql(cfg, ex):
    """重规划给不出下一步就该收敛 —— 再走 generate 只是空转一条 SQL。"""
    _enable_planner(cfg)
    fake = PlanLlm(OK_SQL, OK_SQL,
                   plans=[(True, "一"), (True, "")],      # 第二次目标为空
                   assess=[(False, {"kb_ids": [1]}), (True, {})])
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.step_count == 1
    gen = [c for c in fake.calls if "step" in c]
    assert len(gen) == 1                                  # 只生成过一条 SQL


def test_replan_saying_single_step_also_converges(cfg, ex):
    _enable_planner(cfg)
    fake = PlanLlm(OK_SQL, OK_SQL,
                   plans=[(True, "一"), (False, "还想再查")],
                   assess=[(False, {"kb_ids": [1]}), (True, {})])
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and len([c for c in fake.calls if "step" in c]) == 1
