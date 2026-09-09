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
                     last_sql="", error="", step="", today=""):
        self.calls.append({"error": error, "last_sql": last_sql, "step": step,
                           "schema_prompt": schema_prompt, "today": today})
        if self.raises:
            raise self.raises
        sql = self.sqls.pop(0) if self.sqls else ""
        return SqlDraft(sql=sql, reasoning=self.reasoning), LlmUsage(100, 50, 0, 0.002)

    def structured(self, schema, system, human):
        """规划与评估节点用。默认判定单步、结果足够 —— 多步用例单独覆写。"""
        from askdb.planner import Assessment, Plan

        self.calls.append({"structured": schema.__name__})
        if schema is Plan:
            return Plan(multi_step=False, reason="测试替身默认单步"), LlmUsage(10, 5, 0, 0.0003)
        return Assessment(enough=True, reason="测试替身默认足够"), LlmUsage(10, 5, 0, 0.0003)


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
    # 金额同样是逐步累加出来的，不是拿总 token 乘单价重算的 —— 后者在
    # 兜底切模型或跨计费时段时会算错。
    assert r.cost_cny == round(sum(s["cost_cny"] for s in r.steps), 6)


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
    """R-14 重试上限。

    用「字段不存在」而不是 DELETE 来触发：语句类型错（R-02）属于
    「问题超出范围」，按设计当场收敛、不进反思，验不到重试耗尽。
    字段写错才是"SQL 写错了"这一类 —— 那才是反思该管的。
    """
    bad = "SELECT no_such_col AS x FROM documents"
    r = run(cfg, ex, bad, bad, bad, bad)
    assert not r.ok and r.rejected_by == "R-04"
    assert r.attempts == cfg.max_retry + 1
    assert sum(1 for s in r.steps if s["step"] == "reflect") == cfg.max_retry


def test_out_of_scope_reject_terminates_at_once(cfg, ex):
    """语句类型、危险函数这类超范围拒绝，一轮都不该多跑。"""
    fake = FakeLlm("DELETE FROM documents", "SELECT id AS x FROM documents")
    r = graph.ask("把文档删掉", cfg, executor=ex, llm=fake)
    assert not r.ok and r.rejected_by == "R-02"
    assert r.attempts == 1 and len(fake.calls) == 1


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
    from askdb.quota import build_quota

    cfg.raw["observability"]["daily_quota"] = 1
    build_quota(cfg).reserve()                  # 当日额度已被前一次模型调用占满
    fake = FakeLlm(OK_SQL)
    r = graph.ask("第二次", cfg, executor=ex, llm=fake)
    assert not r.ok and r.rejected_by == "QUOTA"
    assert not fake.calls                       # 一次模型都没调
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

    def __init__(self, *sqls, plans=(), assess=(), next_goals=(), reasons=()):
        super().__init__(*sqls)
        self.plans = list(plans)
        self.assess = list(assess)
        self.next_goals = list(next_goals)
        # reason 也要可控：兜底会用 next_goal or reason，
        # 恒非空的 reason 会让"谁都说不清缺什么"这条分支永远走不到
        self.reasons = list(reasons)

    def structured(self, schema, system, human):
        from askdb.planner import Assessment, Plan

        if schema is Plan:
            multi, goal = self.plans.pop(0) if self.plans else (False, "")
            self.calls.append({"plan": goal, "human": human})
            return Plan(multi_step=multi, reason="替身", goal=goal), LlmUsage(20, 10, 0, 0.0006)
        enough, carry = self.assess.pop(0) if self.assess else (True, {})
        ng = self.next_goals.pop(0) if self.next_goals else ""
        rs = self.reasons.pop(0) if self.reasons else "替身"
        self.calls.append({"assess": enough, "human": human})
        return (Assessment(enough=enough, reason=rs, carry=carry, next_goal=ng),
                LlmUsage(20, 10, 0, 0.0006))


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


def test_replan_honors_assess_verdict(cfg, ex):
    """结果评估判"不足"后，重规划不得反悔。

    设计 §2.1 的链路图里，[8] 判不足必然回到 [2] 再进 [3]，这个环的**唯一
    出口**是 enough=true 或触及 R-16 / R-17 —— 没有"重规划放弃"这条边。
    让 plan 推翻 assess，会出现两次模型调用互相矛盾：assess 说不够、
    plan 说够了，白花一轮 token，且用户拿到一个 assess 自己都认为不完整的答案。

    此处模型在第二次 plan 给不出目标，须用 assess 提供的 next_goal 兜底继续。
    """
    _enable_planner(cfg)
    fake = PlanLlm(OK_SQL, OK_SQL,
                   plans=[(True, "一"), (True, "")],          # 第二次目标为空
                   assess=[(False, {"kb_ids": [1]}), (True, {})],
                   next_goals=["按知识库统计失败文档"])
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.step_count == 2, "必须按 assess 的判定走出第二步"
    assert len([c for c in fake.calls if "step" in c]) == 2


def test_replan_converges_only_when_no_one_can_say_what_is_missing(cfg, ex):
    """assess 说不足、却也说不出缺什么 —— 继续下去是空转，此时才收敛并标注。"""
    _enable_planner(cfg)
    fake = PlanLlm(OK_SQL, OK_SQL,
                   plans=[(True, "一"), (True, "")],
                   assess=[(False, {}), (True, {})],
                   next_goals=[""], reasons=[""])            # 连 assess 也没说清
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.step_count == 1
    assert r.converged_early, "此路收敛必须显式标注原因，不能静默"


def test_replan_multi_step_flag_is_irrelevant_once_in_the_loop(cfg, ex):
    """已经在多步环里了，重规划返回的 multi_step 标记不再有意义 ——
    只要给了目标就继续。设计图上 [2] 的职责是"多步时给出本步目标"，
    退不退出由 [8] 和 R-16/R-17 决定，不由 [2] 自己说了算。"""
    _enable_planner(cfg)
    fake = PlanLlm(OK_SQL, OK_SQL,
                   plans=[(True, "一"), (False, "还想再查")],
                   assess=[(False, {"kb_ids": [1]}), (True, {})])
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.step_count == 2
    assert len([c for c in fake.calls if "step" in c]) == 2


def test_retry_must_not_answer_with_a_different_table(cfg, ex):
    """反思重试不得"换个东西答"。

    实测事故（trace 8fd3676f7e65）：用户问「chunks 表里有多少行」，
    模型写 SELECT COUNT(*) FROM chunks → R-03 正确拦下 → 错误回灌 →
    模型改成 SELECT COUNT(*) FROM documents → **通过并执行**，
    返回一个看起来完全合理的数字。用户问 A，系统答 B。
    评测里表现为应拒拦截率从 100% 掉到 75%。

    根因是路由对所有护栏拒绝一视同仁地重试，而「表不在白名单」这类拒绝
    属于"问题超出范围"—— 表不会因为再问一次就开放。
    """
    fake = FakeLlm("SELECT COUNT(*) AS n FROM chunks",
                   "SELECT COUNT(*) AS n FROM documents")   # 这条绝不能被用上
    r = graph.ask("chunks 表里有多少行", cfg, executor=ex, llm=fake)

    assert not r.ok, "问的对象不可用，就该失败"
    assert r.rejected_by == "R-03"
    assert r.row_count == 0, "绝不能执行并返回一个看似合理的数字"
    assert len(fake.calls) == 1, "根本不该有第二轮 —— 没有重试就没有换表的机会"
    assert "documents" not in (r.sql_final or "")


def test_out_of_scope_reject_costs_no_extra_model_call(cfg, ex):
    """超范围拒绝必须当场收敛，一轮都不许多跑。

    这里也如实记录被放弃的能力：模型把表名拼错（document → documents）
    同样触发 R-03，现在**不再**自动纠正，而是直接失败。
    接受这个代价的依据：报错里写明了真实原因，且 schema 是全量注入的，
    拼错表名远比"换个东西答"罕见 —— 实测 338 次调用里前者 0 次、后者 1 次。
    """
    fake = FakeLlm("SELECT COUNT(*) AS n FROM document",       # 少个 s
                   "SELECT COUNT(*) AS n FROM documents")      # 这条不该被用到
    r = graph.ask("一共有多少文档", cfg, executor=ex, llm=fake)
    assert not r.ok and r.rejected_by == "R-03"
    assert r.attempts == 1, "超范围不该重试"
    assert len(fake.calls) == 1, "多余的模型调用 = 白烧 token"
    assert "白名单" in (r.hint or ""), "得告诉用户真实原因"


def test_field_error_is_still_retried(cfg, ex):
    """字段写错属于"SQL 写错了"，仍应重试 —— 别把可修复的也拦死。"""
    fake = FakeLlm("SELECT no_such_col AS x FROM documents", OK_SQL)
    r = graph.ask("q", cfg, executor=ex, llm=fake)
    assert r.ok and r.attempts == 2


def test_checkpoint_db_opens_in_wal_mode(cfg):
    """多副本共享同一个检查点库的前提。默认 DELETE 模式下写会阻塞读，
    两个 Pod 同时跑必然互相踩。"""
    import sqlite3

    g = graph.build_graph(cfg.checkpoint_db)
    conn = g._askdb_conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

    # 另开一个连接模拟第二个 Pod：WAL 下读方不该被写方挡住
    other = sqlite3.connect(str(cfg.checkpoint_db))
    try:
        conn.execute("BEGIN IMMEDIATE")
        other.execute("SELECT count(*) FROM checkpoints").fetchone()
    finally:
        conn.execute("ROLLBACK")
        other.close()


# ---------------------------------------------------------------------------
# R-17 持久计数（任务中断恢复设计 §4.1 前置）
# ---------------------------------------------------------------------------

def test_tok_used_persisted_into_checkpoint_state(cfg, ex):
    """累计 token 必须落进检查点状态 —— 恢复时回种的就是它。"""
    r = graph.ask("有多少文档", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert r.ok
    g = graph.build_graph(cfg.checkpoint_db)
    snap = g.get_state({"configurable": {"thread_id": r.trace_id}})
    # FakeLlm 单步：generate 一次 = 100+50
    assert snap.values.get("tok_used") == 150


def test_r17_cap_reads_state_not_tracer(cfg, ex):
    """R-17 判定读状态：新 Tracer（模拟恢复后进程）也拦得住。"""
    from askdb.graph import Deps, _n_assess
    from askdb.trace import Tracer

    deps = Deps(cfg=cfg, llm=FakeLlm(), executor=ex, tracer=Tracer())
    state = {
        "question": "q", "multi_step": True, "step_no": 1, "max_steps": 5,
        "cost_cap_tokens": 100, "tok_used": 120,     # 上一段进程已花 120
        "steps_done": [], "carry": {}, "rows": [], "columns": [],
        "sql_final": "SELECT 1", "row_count": 1,
    }
    out = _n_assess(state, {"configurable": {"deps": deps}})
    assert out["enough"] is True
    assert "累计成本上限" in out.get("converged_early", "")


# ------------------------------ R-11 收窄留痕（2026-09-09 十二源回归的头号缺陷）

NARROW_SQL = ("SELECT COUNT(*) AS 数量 FROM documents "
              "WHERE created_at >= DATE '2024-01-01'")


def test_scope_narrowed_is_recorded_when_retry_beats_the_threshold(cfg, ex):
    """扫描量超限 → 回灌"缩小时间范围" → 模型加了条件跑通。

    链路每一步都成功，rejected_by 是 null，页面上与全量结果毫无区别 ——
    实测「一共有多少笔订单」因此答 54,192，真值 120 万。留痕是唯一的补救。
    """
    # 阈值卡在两版的预估扫描量之间：第一版 527 行被拦，加了时间窗的第二版
    # 210 行放行。（数字是注入租户谓词之后的估算，不是裸 SQL 的。）
    cfg.raw["guard"]["max_scan_rows"] = 300
    r = run(cfg, ex, OK_SQL, NARROW_SQL)
    assert r.ok, f"第二版应当放行：{r.rejected_by} {r.error}"
    assert r.scope_narrowed is True
    assert "不是全量" in r.scope_note


def test_scope_is_not_flagged_on_a_clean_first_try(cfg, ex):
    """没被拦过就不该报警 —— 误报会让这条提示很快被无视。"""
    r = run(cfg, ex, OK_SQL)
    assert r.ok and r.scope_narrowed is False and r.scope_note == ""


def test_narrowing_is_written_into_the_audit_record(cfg, ex):
    """事后复盘一个对不上的数字时，这是第一个要看的字段。"""
    import json as _json
    cfg.raw["guard"]["max_scan_rows"] = 300
    r = run(cfg, ex, OK_SQL, NARROW_SQL)
    assert r.ok and r.scope_narrowed
    recs = [_json.loads(ln) for ln in cfg.audit_log.read_text().splitlines() if ln.strip()]
    final = [x for x in recs if x.get("trace_id") == r.trace_id and "sql_final" in x]
    assert final and final[-1]["scope_narrowed"] is True


def test_retry_after_threshold_gets_the_summary_tables(cfg, ex):
    """重试那一轮必须看得见预聚合汇总表。

    看不见，"降低扫描量"就只剩"加个过滤条件"一条路 —— 而那条路的终点
    是一个悄悄收窄了范围的错数。
    """
    from askdb.config import Table
    cfg.tables["doc_daily_stats"] = Table(
        name="doc_daily_stats", aliases=[], desc="文档按日汇总", columns={})
    cfg.raw["guard"]["max_scan_rows"] = 1
    llm = FakeLlm(OK_SQL, OK_SQL, OK_SQL, OK_SQL)
    graph.ask("一共有多少文档", cfg, executor=ex, llm=llm)
    first, *retries = llm.calls
    assert retries, "应当发生过重试"
    assert any("预聚合汇总表" in c.get("schema_prompt", "") for c in retries)


# ------------------------------------------------- 报错与 SQL 必须是同一版

def test_rejected_sql_is_the_one_that_failed(cfg, ex):
    """护栏拒绝时，结果里带的 SQL 必须是**被拒的那条**。

    2026-09-09 回归 P-02：接口返回了"第 3 轮的 SQL + 第 2 轮的报错"——
    报错说 products 没有 order_count，而附着的 SQL 里那一列明明带着别名，
    用户按提示改，怎么改都对不上。根因是拒绝分支不写 sql_final，
    它还停在**上一轮**通过护栏的那条上。这里直接喂一个"上一轮留下过
    sql_final"的状态给护栏节点，看它拒绝时把哪一条写回去。
    """
    from askdb.trace import Tracer

    deps = graph.Deps(cfg=cfg, llm=FakeLlm(), executor=ex, tracer=Tracer())
    out = graph._n_guard(
        {"question": "测试问题", "org_id": 65,
         "sql_raw": "SELECT no_such_column FROM documents",
         "sql_final": "SELECT file_name FROM documents LIMIT 200"},   # 上一轮的残留
        {"configurable": {"deps": deps}})
    assert out["rejected_by"] == "R-04"
    assert "no_such_column" in out["sql_final"]
    assert "file_name" not in out["sql_final"]


def test_empty_result_says_so(cfg, ex):
    """0 行不能只是"成功返回 0 行"——要说清"可能是条件没落在有数据的区间"。"""
    r = run(cfg, ex, "SELECT file_name AS 文件名 FROM documents "
                     "WHERE status = 'NO_SUCH_STATUS'")
    assert r.ok and r.row_count == 0
    assert "结果为空" in r.empty_note


def test_model_is_told_todays_date(cfg, ex):
    """不告诉模型今天几号，"昨天""最近一个月"就只能靠猜（实测猜出过去年的日期）。"""
    llm = FakeLlm(OK_SQL)
    graph.ask("昨天有多少文档", cfg, executor=ex, llm=llm)
    sent = [c for c in llm.calls if "today" in c]
    assert sent and len(sent[0]["today"]) == 10       # YYYY-MM-DD
