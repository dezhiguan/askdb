"""可信数据 Agent 运行时（tools / skill / agent 循环 / 异步）单元测试。

全部自足：合成 Config + 打桩 LLM/执行器，不连真实库或模型。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from askdb import agent as A
from askdb import grounding
from askdb import async_runner, skill, tools
from askdb.config import Column, Config, Table
from askdb.llm import LlmUsage


# --------------------------------------------------------------------------
# 合成 cfg / 桩
# --------------------------------------------------------------------------
def _cfg(tmp_path: Path, **extra) -> Config:
    raw = {
        "tenant": {"default_ctx": 316},
        "guard": {"max_scan_rows": 200},
        "datasource": {"type": "duckdb", "path": ":memory:"},
        "llm": {"model": "fake"},
        "observability": {"store": "file", "audit_log": str(tmp_path / "audit.jsonl")},
    }
    raw.update(extra)
    cols = {
        "id": Column(name="id", type="BIGINT"),
        "chunk_type": Column(name="chunk_type", type="VARCHAR", enum=["JD", "COMPANY"]),
        "phone": Column(name="phone", type="VARCHAR"),
    }
    t = Table(name="documents", desc="文档", aliases=["文件"], columns=cols)
    return Config(root=tmp_path, raw=raw, tables={"documents": t}, metrics=[], path="t.yaml")


class _Q:
    limit = 12000

    def exhausted(self):
        return (False, 0)


class _FakeExec:
    """执行器桩：explain 可配行数，run 返回固定结果。"""

    def __init__(self, est_rows=5):
        self._est = est_rows

    def explain(self, sql):
        return type("E", (), {"est_rows": self._est})()

    def set_org(self, o):
        pass

    def run(self, sql, limit_capped=None):
        return type("QR", (), {
            "columns": ["n"], "rows": [[42]], "row_count": 1, "truncated": False,
            "elapsed_ms": 1, "as_of": "", "mask_degraded": False, "masked_columns": [],
        })()


class _FakeLLM:
    """脚本化 LLM：intent 可答，之后按 actions 逐个吐。"""

    def __init__(self, actions):
        self._actions = list(actions)
        self._i = 0

    @property
    def model_name(self):
        return "fake"

    def structured(self, schema, system, human):
        u = LlmUsage(input_tokens=100, output_tokens=20, cost_cny=0.001)
        if schema is A.IntentCheck:
            return schema(answerable=True, out_of_scope=False, reason="可答"), u
        act = self._actions[min(self._i, len(self._actions) - 1)]
        self._i += 1
        return schema(**act), u


# --------------------------------------------------------------------------
# skill
# --------------------------------------------------------------------------
def test_skill_render_has_general_and_extra(tmp_path):
    cfg = _cfg(tmp_path, skill={"rules": ["部署口径：营收以 finance.revenue 为准"]})
    text = skill.render(cfg)
    assert "方法论" in text and "供应商" in text  # 通用规则
    assert "finance.revenue" in text            # 部署追加
    assert len(skill.rules(cfg)) == len(skill.GENERAL_RULES) + 1


# --------------------------------------------------------------------------
# tools：只读原子
# --------------------------------------------------------------------------
def test_get_table_schema_case_insensitive_and_sensitive(tmp_path):
    cfg = _cfg(tmp_path)
    ctx = tools.ToolContext(cfg=cfg, org_id=0)
    r = tools.invoke("get_table_schema", {"table": "DOCUMENTS"}, ctx)
    assert r.ok and r.data["table"] == "documents"
    sens = [c["name"] for c in r.data["columns"] if c["sensitive"]]
    assert "phone" in sens  # looks_sensitive 自动标记
    enums = {c["name"]: c["enum"] for c in r.data["columns"]}
    assert enums["chunk_type"] == ["JD", "COMPANY"]


def test_get_table_schema_missing(tmp_path):
    cfg = _cfg(tmp_path)
    r = tools.get_table_schema("nope", cfg)
    assert not r.ok and "documents" in r.data["available"]


def test_unknown_tool_and_missing_arg(tmp_path):
    cfg = _cfg(tmp_path)
    ctx = tools.ToolContext(cfg=cfg, org_id=0)
    assert not tools.invoke("no_such", {}, ctx).ok
    assert "缺少参数" in tools.invoke("get_table_schema", {}, ctx).error


def test_tool_specs_default_excludes_side_effect():
    names = {s["name"] for s in tools.tool_specs()}
    assert {"search_schema", "get_table_schema", "execute_sql", "analyze_result"} <= names
    assert "export_result" not in names  # 副作用默认不暴露


# --------------------------------------------------------------------------
# tools：execute_sql 护栏 / waiver / 脱敏
# --------------------------------------------------------------------------
def _stub_guard(monkeypatch, ok=True, rejected_by=None):
    class G:
        pass
    g = G()
    g.ok = ok; g.sql = "SELECT 1"; g.rejected_by = rejected_by; g.reason = "x"
    g.rules_fired = []; g.rewrites = []; g.out_of_scope = False
    monkeypatch.setattr(tools.guard, "check", lambda *a, **k: g)


def test_execute_sql_guard_reject(tmp_path, monkeypatch):
    _stub_guard(monkeypatch, ok=False, rejected_by="R-02")
    cfg = _cfg(tmp_path)
    r = tools.execute_sql("DROP TABLE t", cfg, 0, executor=_FakeExec())
    assert not r.ok and r.rejected_by == "R-02"


def test_execute_sql_r11_over_scan(tmp_path, monkeypatch):
    _stub_guard(monkeypatch)
    cfg = _cfg(tmp_path)
    r = tools.execute_sql("SELECT 1", cfg, 0, executor=_FakeExec(est_rows=999999))
    assert not r.ok and r.rejected_by == "R-11" and r.data["needs_approval"]


def test_execute_sql_scan_waiver_bypasses_r11(tmp_path, monkeypatch):
    _stub_guard(monkeypatch)
    cfg = _cfg(tmp_path, _scan_waiver=True)
    r = tools.execute_sql("SELECT 1", cfg, 0, executor=_FakeExec(est_rows=999999))
    assert r.ok and r.data["rows"] == [[42]]


def test_execute_sql_ok(tmp_path, monkeypatch):
    _stub_guard(monkeypatch)
    cfg = _cfg(tmp_path)
    r = tools.execute_sql("SELECT 1", cfg, 0, executor=_FakeExec())
    assert r.ok and r.data["row_count"] == 1


def test_execute_sql_datasource_error_on_explain(tmp_path, monkeypatch):
    """干跑阶段库连不上（不可重试）→ DATASOURCE + fatal，不继续往下跑。"""
    from askdb.executor import DataSourceError
    _stub_guard(monkeypatch)

    class ExplainDead(_FakeExec):
        def explain(self, sql):
            raise DataSourceError("连接被拒", hint="查连通性", retryable=False)

    r = tools.execute_sql("SELECT 1", _cfg(tmp_path), 0, executor=ExplainDead())
    assert not r.ok and r.rejected_by == "DATASOURCE"
    assert r.data["fatal"] is True and r.data["hint"] == "查连通性"


def test_execute_sql_explain_soft_error_continues(tmp_path, monkeypatch):
    """explain 抛普通异常（非 DataSourceError）→ 吞掉，照常执行。"""
    _stub_guard(monkeypatch)

    class ExplainFlaky(_FakeExec):
        def explain(self, sql):
            raise RuntimeError("explain 不支持")

    r = tools.execute_sql("SELECT 1", _cfg(tmp_path), 0, executor=ExplainFlaky())
    assert r.ok and r.data["explain_rows"] is None


def test_execute_sql_mask_unresolved_rejected(tmp_path, monkeypatch):
    """run 阶段脱敏判定不出来源 → P03 从严拒绝。"""
    from askdb.executor import MaskUnresolved
    _stub_guard(monkeypatch)

    class MaskDead(_FakeExec):
        def run(self, sql, limit_capped=None):
            raise MaskUnresolved("解析不出投影")

    r = tools.execute_sql("SELECT 1", _cfg(tmp_path), 0, executor=MaskDead())
    assert not r.ok and r.rejected_by == "P03"


def test_execute_sql_run_datasource_and_generic_error(tmp_path, monkeypatch):
    """run 阶段可重试超时 → DATASOURCE 非 fatal；未知异常 → 兜底错。"""
    from askdb.executor import DataSourceError
    _stub_guard(monkeypatch)

    class TimeoutRun(_FakeExec):
        def run(self, sql, limit_capped=None):
            raise DataSourceError("语句超时", hint="缩小范围", retryable=True)

    r = tools.execute_sql("SELECT 1", _cfg(tmp_path), 0, executor=TimeoutRun())
    assert not r.ok and r.rejected_by == "DATASOURCE" and r.data["fatal"] is False

    class BoomRun(_FakeExec):
        def run(self, sql, limit_capped=None):
            raise ValueError("未知")

    r2 = tools.execute_sql("SELECT 1", _cfg(tmp_path), 0, executor=BoomRun())
    assert not r2.ok and r2.rejected_by is None and "未知" in r2.error


# --------------------------------------------------------------------------
# tools：analyze / export（第 2/3 层）
# --------------------------------------------------------------------------
def _ctx_with_result(tmp_path):
    cfg = _cfg(tmp_path)
    return tools.ToolContext(cfg=cfg, org_id=0, last_result={
        "columns": ["org", "cnt", "phone"],
        "rows": [["a", 10, "1***2"], ["b", 30, "1***3"], ["a", 5, None]],
        "masked_columns": ["phone"]})


def test_analyze_result_stats_skip_masked(tmp_path):
    ctx = _ctx_with_result(tmp_path)
    r = tools.invoke("analyze_result", {}, ctx)
    assert r.ok
    by = {s["column"]: s for s in r.data["stats"]}
    assert by["cnt"]["sum"] == 45.0 and by["cnt"]["max"] == 30.0
    assert "min" not in by["phone"]  # 脱敏列跳过数值统计
    assert by["org"]["distinct"] == 2


def test_analyze_result_needs_data(tmp_path):
    cfg = _cfg(tmp_path)
    r = tools.analyze_result(tools.ToolContext(cfg=cfg, org_id=0))
    assert not r.ok


def test_side_effect_blocked_from_llm_but_runs_when_approved(tmp_path):
    ctx = _ctx_with_result(tmp_path)
    # LLM 直接 invoke 被 tier 闸拦
    blocked = tools.invoke("export_result", {"format": "csv"}, ctx)
    assert not blocked.ok and blocked.note == "HITL required"
    # 未确认的 run_side_effect 也拒
    assert not tools.run_side_effect("export_result", {}, ctx, approved=False).ok
    # 确认后放行
    ok = tools.run_side_effect("export_result", {"format": "csv"}, ctx, approved=True)
    assert ok.ok and ok.data["rows"] == 3 and "org,cnt,phone" in ok.data["payload"]
    # 非副作用工具不走这条路
    assert not tools.run_side_effect("search_schema", {}, ctx, approved=True).ok


# --------------------------------------------------------------------------
# agent 循环
# --------------------------------------------------------------------------
def _patch_recall(monkeypatch):
    monkeypatch.setattr(tools, "search_schema", lambda q, c: tools.ToolResult(
        ok=True, tool="search_schema",
        data={"tables": ["documents"], "prompt": "【可用的表】documents", "blind": False}))


def _patch_exec_invoke(monkeypatch, result=None, rejected_by=None):
    def fake(name, args, ctx):
        if rejected_by:
            return tools.ToolResult(ok=False, tool="execute_sql", rejected_by=rejected_by,
                                    error="预估扫描过大",
                                    data={"sql_final": "SELECT 1", "explain_rows": 999999})
        return tools.ToolResult(ok=True, tool="execute_sql", data=result or {
            "sql_final": "SELECT COUNT(*) FROM documents WHERE chunk_type='JD'",
            "columns": ["n"], "rows": [[15669]], "row_count": 1, "masked_columns": [],
            "rules_fired": [], "rewrites": [], "explain_rows": 15})
    monkeypatch.setattr(tools, "invoke", fake)


def test_agent_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    _patch_exec_invoke(monkeypatch)
    llm = _FakeLLM([
        {"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}},
        {"finish": True, "answer": "JD 文档共 15669 条（口径 chunk_type='JD'）"},
    ])
    r = A.run_agent("JD 文档有多少", _cfg(tmp_path), 316, executor=_FakeExec(), llm=llm)
    assert r.ok and r.rows == [[15669]]
    assert "chunk_type" in r.reasoning
    steps = [s["step"] for s in r.steps]
    assert "schema_recall" in steps and "intent" in steps and "tool_call" in steps


def test_agent_out_of_scope_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)

    class OOS(_FakeLLM):
        def structured(self, schema, system, human):
            u = LlmUsage(input_tokens=10, output_tokens=5, cost_cny=0.0001)
            assert schema is A.IntentCheck  # 越域不该进循环
            return schema(answerable=False, out_of_scope=True, reason="库里没有供应商实体"), u

    r = A.run_agent("多少供应商", _cfg(tmp_path), 316, executor=_FakeExec(), llm=OOS([]))
    assert not r.ok and r.rejected_by == "OOS"


def test_agent_clarify(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)

    class C(_FakeLLM):
        def structured(self, schema, system, human):
            u = LlmUsage(input_tokens=10, output_tokens=5, cost_cny=0.0001)
            return schema(answerable=False, out_of_scope=False, clarify="缺主语"), u

    r = A.run_agent("第二名呢", _cfg(tmp_path), 316, executor=_FakeExec(), llm=C([]))
    assert not r.ok and r.rejected_by == "CLARIFY"


def test_agent_r11_surfaces_for_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    _patch_exec_invoke(monkeypatch, rejected_by="R-11")
    llm = _FakeLLM([{"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}}])
    r = A.run_agent("大查询", _cfg(tmp_path), 316, executor=_FakeExec(), llm=llm)
    assert not r.ok and r.rejected_by == "R-11" and r.explain_rows == 999999


def test_agent_quota_exhausted(tmp_path, monkeypatch):
    class Over:
        limit = 10
        def exhausted(self):
            return (True, 10)
    monkeypatch.setattr(A, "build_quota", lambda c: Over())
    r = A.run_agent("x", _cfg(tmp_path), 316, executor=_FakeExec(), llm=_FakeLLM([]))
    assert not r.ok and r.rejected_by == "QUOTA"


def test_agent_step_cap_converges(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    _patch_exec_invoke(monkeypatch)
    # 永不 finish → 触 max_steps=1 上限收敛
    llm = _FakeLLM([{"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}}])
    r = A.run_agent("x", _cfg(tmp_path, agent={"max_steps": 1}), 316,
                    executor=_FakeExec(), llm=llm)
    assert r.converged_early and "上限" in r.converged_early


def test_agent_writes_audit(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    _patch_exec_invoke(monkeypatch)
    cfg = _cfg(tmp_path)
    llm = _FakeLLM([
        {"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}},
        {"finish": True, "answer": "done"},
    ])
    A.run_agent("x", cfg, 316, executor=_FakeExec(), llm=llm)
    recs = [json.loads(x) for x in open(tmp_path / "audit.jsonl")]
    phases = {r.get("phase", "final") for r in recs}
    assert "started" in phases and "final" in phases


def test_agent_intent_quota_and_llm_error(tmp_path, monkeypatch):
    """意图预检阶段：配额耗尽 → QUOTA；其他异常 → LLM。"""
    from askdb.quota import QuotaExceeded
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)

    class Boom(_FakeLLM):
        def __init__(self, exc):
            super().__init__([])
            self._exc = exc

        def structured(self, schema, system, human):
            raise self._exc

    r = A.run_agent("x", _cfg(tmp_path), 316, executor=_FakeExec(),
                    llm=Boom(QuotaExceeded(10, 10, "今日额度已用尽")))
    assert not r.ok and r.rejected_by == "QUOTA"
    r2 = A.run_agent("x", _cfg(tmp_path), 316, executor=_FakeExec(),
                     llm=Boom(RuntimeError("网络断了")))
    assert not r2.ok and r2.rejected_by == "LLM"


def test_agent_decide_quota_and_llm_error(tmp_path, monkeypatch):
    """自主循环里决策调用失败：配额 → 收敛作答；其他异常 → LLM。"""
    from askdb.quota import QuotaExceeded
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)

    class DecideBoom(_FakeLLM):
        def __init__(self, exc):
            super().__init__([])
            self._exc = exc

        def structured(self, schema, system, human):
            if schema is A.IntentCheck:
                return schema(answerable=True, out_of_scope=False, reason="可答"), \
                    LlmUsage(input_tokens=1, output_tokens=1, cost_cny=0.0)
            raise self._exc

    r = A.run_agent("x", _cfg(tmp_path), 316, executor=_FakeExec(),
                    llm=DecideBoom(QuotaExceeded(10, 10, "额度用尽")))
    assert r.converged_early and "配额" in r.converged_early
    r2 = A.run_agent("x", _cfg(tmp_path), 316, executor=_FakeExec(),
                     llm=DecideBoom(RuntimeError("挂了")))
    assert not r2.ok and r2.rejected_by == "LLM"


def test_agent_datasource_fatal_stops(tmp_path, monkeypatch):
    """execute_sql 报不可重试的数据源错 → 直接 DATASOURCE，不烧预算。"""
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)

    def fake(name, args, ctx):
        return tools.ToolResult(ok=False, tool="execute_sql", rejected_by="DATASOURCE",
                                error="连接被拒", data={"fatal": True, "hint": "查连通性"})
    monkeypatch.setattr(tools, "invoke", fake)
    llm = _FakeLLM([{"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}}])
    r = A.run_agent("x", _cfg(tmp_path), 316, executor=_FakeExec(), llm=llm)
    assert not r.ok and r.rejected_by == "DATASOURCE" and "查连通性" in (r.hint or "")


def test_agent_schema_tools_recorded_in_history(tmp_path, monkeypatch):
    """search_schema / get_table_schema 命中后把命中表/列写进历史（非 execute_sql 分支）。"""
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)

    def fake(name, args, ctx):
        if name == "get_table_schema":
            return tools.ToolResult(ok=True, tool="get_table_schema",
                                    data={"table": "documents",
                                          "columns": [{"name": "chunk_type"}, {"name": "id"}]})
        if name == "search_schema":
            return tools.ToolResult(ok=True, tool="search_schema",
                                    data={"tables": ["documents"], "prompt": "p"})
        return tools.ToolResult(ok=True, tool="execute_sql", data={
            "sql_final": "SELECT 1", "columns": ["n"], "rows": [[7]], "row_count": 1,
            "masked_columns": [], "rules_fired": [], "rewrites": [], "explain_rows": 1})
    monkeypatch.setattr(tools, "invoke", fake)
    llm = _FakeLLM([
        {"finish": False, "tool": "search_schema", "args": {"question": "q"}},
        {"finish": False, "tool": "get_table_schema", "args": {"table": "documents"}},
        {"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}},
        {"finish": True, "answer": "共 7 条"},
    ])
    r = A.run_agent("多少", _cfg(tmp_path, agent={"max_steps": 6}), 316,
                    executor=_FakeExec(), llm=llm)
    assert r.ok and r.rows == [[7]]


def test_agent_no_evidence_blocks_fabricated_number(tmp_path, monkeypatch):
    """一次成功查询都没有、答案却带数字 → NO_EVIDENCE，且不回显编造内容。"""
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    llm = _FakeLLM([{"finish": True, "answer": "大约有 120 万条订单"}])
    r = A.run_agent("订单总数", _cfg(tmp_path, agent={"max_steps": 2}), 316,
                    executor=_FakeExec(), llm=llm)
    assert not r.ok and r.rejected_by == "NO_EVIDENCE"
    assert "120" not in (r.reasoning or "") and "120" not in (r.error or "")


def test_agent_no_result_when_empty(tmp_path, monkeypatch):
    """没查询也没答案 → NO_RESULT。"""
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    llm = _FakeLLM([{"finish": True, "answer": ""}])
    r = A.run_agent("x", _cfg(tmp_path, agent={"max_steps": 2}), 316,
                    executor=_FakeExec(), llm=llm)
    assert not r.ok and r.rejected_by == "NO_RESULT"


def test_agent_qualitative_answer_without_number_passes(tmp_path, monkeypatch):
    """无数字的定性回答（没可编造的量）即使没跑 SQL 也放行。"""
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    llm = _FakeLLM([{"finish": True, "answer": "本库主要围绕文档与切片，没有订单相关的表。"}])
    r = A.run_agent("有订单表吗", _cfg(tmp_path, agent={"max_steps": 2}), 316,
                    executor=_FakeExec(), llm=llm)
    assert r.ok and "文档" in r.reasoning


def test_agent_grounding_enforce_retries_then_grounds(tmp_path, monkeypatch):
    """enforce 档：结论数字追溯不到 → 回灌重查一轮；查到后放行。"""
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    _patch_exec_invoke(monkeypatch, result={
        "sql_final": "SELECT COUNT(*)", "columns": ["n"], "rows": [[15669]],
        "row_count": 1, "masked_columns": [], "rules_fired": [], "rewrites": [],
        "explain_rows": 1})
    # 先查一次（拿到 15669）再 finish 报一个对不上的数 → 触发接地重试；重查后用真实数收尾。
    llm = _FakeLLM([
        {"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}},
        {"finish": True, "answer": "JD 有 99999 条"},
        {"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1"}},
        {"finish": True, "answer": "JD 共 15669 条"},
    ])
    r = A.run_agent("JD 多少", _cfg(tmp_path, agent={"max_steps": 5, "grounding": "enforce"}),
                    316, executor=_FakeExec(), llm=llm)
    assert r.ok and "15669" in r.reasoning
    assert any(s["step"] == "grounding" for s in r.steps)


# --------------------------------------------------------------------------
# async_runner
# --------------------------------------------------------------------------
def test_async_fast_returns_sync():
    r, notice = async_runner.run_or_detach(lambda: "R", 500, "tid")
    assert r == "R" and notice is None


def test_async_slow_detaches():
    def slow():
        time.sleep(0.5)
        return "done"
    r, notice = async_runner.run_or_detach(slow, 100, "tid2")
    assert r is None and notice["async"] and notice["thread_id"] == "tid2"


def test_async_immediate():
    r, notice = async_runner.run_or_detach(lambda: "R", 0, "tid3")
    assert r is None and notice["async"]


def test_handoff_node_boundary_requests_early():
    """节点边界的三条提前判据。**它们在阈值之前就能定** —— 多等一秒都是白等。"""
    ho = async_runner.new_handoff(60_000)
    ho.check(step=3)
    assert "多步" in ho.reason and ho.wake.is_set()

    ho2 = async_runner.new_handoff(60_000)
    ho2.check(step=1, tok_used=6000, cost_cap=12000)
    assert "token" in ho2.reason

    ho3 = async_runner.new_handoff(60_000)
    ho3.check(step=1, explain_rows=600_000, scan_threshold=1_000_000)
    assert "扫描" in ho3.reason


def test_handoff_reason_is_first_one():
    """理由**不被覆盖**：回执里那句话要与审计对得上。"""
    ho = async_runner.new_handoff(60_000)
    ho.check(step=3)
    ho.check(step=1, tok_used=99999, cost_cap=100)
    assert "多步" in ho.reason


def test_handoff_detaches_before_threshold():
    """节点边界请求交接 → 入口那一等**立刻**结束，不等满阈值。"""
    ho = async_runner.new_handoff(30_000)

    def slow():
        ho.request("多步链路")
        time.sleep(0.5)
        return "done"

    t0 = time.monotonic()
    r, notice = async_runner.run_or_detach(slow, 30_000, "tid4", handoff=ho)
    assert r is None and notice["reason"] == "多步链路"
    assert time.monotonic() - t0 < 5      # 远小于 30s 阈值，说明是被叫醒的

def test_async_per_user_cap_rejects_not_queues():
    """每账号在跑上限：**拒绝**，不排队。排队会让"正在后台执行"变成谎话。"""
    ho = async_runner.new_handoff(0)
    async_runner.run_or_detach(lambda: time.sleep(0.4), 0, "t1", user="u1",
                               per_user=1, handoff=ho)
    with pytest.raises(async_runner.CapacityExceeded):
        async_runner.run_or_detach(lambda: "R", 0, "t2", user="u1", per_user=1)
    # 别人不受影响 —— 上限是按账号算的
    r, notice = async_runner.run_or_detach(lambda: "R", 0, "t3", user="u2",
                                           per_user=1)
    assert notice["async"]


def test_async_pool_full_runs_inline():
    """全池满：不交接、同步跑到底。宁可这一条慢，也不能因为没槽位就把它丢掉。"""
    pool = async_runner._Pool(size=1)
    saved = async_runner._POOL
    async_runner._POOL = pool
    try:
        async_runner.run_or_detach(lambda: time.sleep(0.4), 0, "t1", user="a")
        r, notice = async_runner.run_or_detach(lambda: "R", 0, "t2", user="b")
        assert r == "R" and notice is None
    finally:
        async_runner._POOL = saved


def test_handoff_stashes_only_when_detached():
    """暂存只发生在真交接出去的那些上 —— 同步返回的那次不必多写一遍。"""
    got: list[str] = []
    async_runner.run_or_detach(lambda: "R", 500, "t-sync",
                               on_detached_done=lambda r: got.append(r))
    assert got == []
    ho = async_runner.new_handoff(0)
    async_runner.run_or_detach(lambda: "R", 0, "t-async", handoff=ho,
                               on_detached_done=lambda r: got.append(r))
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got == ["R"]


# --------------------------------------------------------------------------
# grounding —— 结论里的数字接不接地（BUG-A5）
#
# 这一层的两类错代价完全不对称：漏判一个编造只是回到现状，误判一个正确答案是
# 直接毁掉一个对的回答。所以下面的用例里「该放行」的比「该抓住」的多得多 ——
# 它们钉的是**不许误伤**，那才是这层最容易出事的方向。
# --------------------------------------------------------------------------
def _r(rows, columns=None):
    return {"columns": columns or [], "rows": rows}


def test_grounding_flags_fabricated_numbers():
    """跑的是探查查询，答的是另一回事 —— 正是 2026-09-11 复测抓到的那两条。"""
    bad = grounding.ungrounded(
        "2026年8月 GMV 合计 1,347,590.05 元，该月 181,164 行明细；表整体 2,012,920 行。",
        [_r([["2026-06-10", "2026-09-08", 2012920]], ["min_d", "max_d", "n"])])
    assert bad == [1347590.05, 181164.0]
    # 2,012,920 确实在返回行里，不该被点名
    assert 2012920.0 not in bad


def test_grounding_allows_values_straight_from_rows():
    assert grounding.ungrounded("订单总数 1,200,000 笔。", [_r([[1200000]])]) == []


def test_grounding_allows_one_step_arithmetic():
    """"全表 447,000、可售 398,082、停售 48,918" —— 最后一个是模型自己减的。

    禁掉一次算术会把这类完全正确的答案判成编造，那正是这层最该避免的失败。
    """
    assert grounding.ungrounded(
        "全表 447,000 条，可售 398,082 条，停售 48,918 条。",
        [_r([[447000, 398082]])]) == []


def test_grounding_allows_column_total():
    """分项相加出来的合计，库里没有单独一行，但它确实来自返回值。"""
    assert grounding.ungrounded(
        "RESOLVED 106,303、CLOSED 16,549、PROCESSING 15,094，合计 137,946 条。",
        [_r([["RESOLVED", 106303], ["CLOSED", 16549], ["PROCESSING", 15094]])]) == []


def test_grounding_allows_a_partial_column_sum():
    """只加其中几项也是派生值。

    2026-09-12 影子跑测的第一个误判就是这条：模型答"排除已解决/已关闭之后
    仍在流程中的 = 15,094 + 8,246 + 3,808 = 27,148"，完全正确，却因为
    两两一次算术覆盖不到三项求和而被点名。
    """
    rows = [["RESOLVED", 106303], ["CLOSED", 16549], ["PROCESSING", 15094],
            ["PENDING", 8246], ["ESCALATED", 3808]]
    assert grounding.ungrounded(
        "五项合计 150,000 条；仍在流程中的为 15,094 + 8,246 + 3,808 = 27,148 条。",
        [_r(rows)]) == []


def test_grounding_still_catches_fabrication_after_subset_sums():
    """放宽到子集和之后，编造仍然要抓得住 —— 否则这层就白设了。"""
    assert grounding.ungrounded(
        "8 月 GMV 合计 1,347,590.05 元，181,164 行。",
        [_r([["2026-06-10", "2026-09-08", 2012920]])]) == [1347590.05, 181164.0]


def test_grounding_does_not_read_sql_commas_as_thousands():
    """`NULLIF(782738,0)` 里的逗号是参数分隔符，不是千分位。

    读成 782738,0 → 7,827,380 会把一个完全正确的答案点名（第四轮 R4）。
    """
    assert grounding.numbers_in("ROUND(100.0 * 242754 / NULLIF(782738,0), 4)") == [
        100.0, 242754.0, 782738.0, 0.0, 4.0]
    assert grounding.numbers_in("共 1,200,000 笔") == [1200000.0]


def test_grounding_allows_digits_inside_returned_names():
    """活动名「双112025第6期」是库里查出来的，照抄它不该被当成编造。"""
    assert grounding.ungrounded(
        "预算第 2 名是「双112025第6期」，799,645.15 元。",
        [_r([[1, "开学季2025第8期", "799843.46"], [2, "双112025第6期", "799645.15"]])]) == []


def test_grounding_allows_ratio_of_two_returned_values():
    assert grounding.ungrounded(
        "不良率 = 64,369 / 3,761,321 = 1.7113%。", [_r([[64369, 3761321]])]) == []


def test_grounding_skips_years_and_small_numbers():
    """年份、占比、天数一律不查 —— 它们几乎总是就地算的，查了只会制造误判。"""
    assert grounding.ungrounded(
        "2026年8月共 31 天，占比 77.84%，平均 4.29 星，排名第 2。", [_r([[31]])]) == []


def test_grounding_tolerates_rounding():
    assert grounding.ungrounded("平均 2215.61 元。", [_r([[2215.6134]])]) == []


def test_grounding_stays_out_when_nothing_ran():
    """一条结果都没有时不归这层管 —— 那是 NO_EVIDENCE 的事，免得一件事两种说法。"""
    assert grounding.ungrounded("一共 10,000 个。", []) == []
    assert grounding.ungrounded("一共 10,000 个。", [_r([])]) == []


def test_grounding_parses_awkward_number_text():
    assert grounding.numbers_in("共 1,200,000 笔，占 3.5%，尾号 12.") == [1200000.0, 3.5, 12.0]
    assert grounding.numbers_in("") == []


def test_grounding_reads_decimal_and_blank_cells():
    """PG 的 numeric 经驱动回来是 Decimal，空串是"这一格没有值"而不是 0。"""
    from decimal import Decimal
    vals = grounding.values_of([_r([[Decimal("189730349.49"), "", object()]])])
    assert 189730349.49 in vals and len(vals) == 1


def test_grounding_ignores_non_numeric_cells():
    """布尔、None、文本列都不该被当成数值来源。"""
    vals = grounding.values_of([_r([["ONLINE", None, True, "86,990"], ["APP", None, False, "abc"]])])
    assert 86990.0 in vals and True not in [v for v in vals if isinstance(v, bool)]


def test_grounding_fmt_reads_like_a_number():
    assert grounding.fmt([1347590.05, 181164.0]) == "1,347,590.05、181,164"
    assert grounding.fmt([]) == ""


# --------------------------------------------------------------------------
# grounding 在 agent 循环里的三档行为
# --------------------------------------------------------------------------
_PROBE = {"sql_final": "SELECT MIN(a), MAX(a), COUNT(*) FROM documents",
          "columns": ["min_a", "max_a", "n"], "rows": [["x", "y", 2012920]],
          "row_count": 1, "masked_columns": [], "rules_fired": [], "rewrites": [],
          "explain_rows": 15, "truncated": False}
_REAL = dict(_PROBE, columns=["gmv"], rows=[[7693056921.16]])
_FAKE_ANSWER = {"finish": True, "answer": "8 月 GMV 合计 1,347,590.05 元，181,164 行。"}
_GOOD_ANSWER = {"finish": True, "answer": "8 月 GMV 合计 7,693,056,921.16 元。"}
_DO_EXEC = {"finish": False, "tool": "execute_sql", "args": {"sql": "SELECT 1 FROM documents"}}
#: 接地校验点名之后模型**换一条 SQL** 去补查。必须与 _DO_EXEC 不同：
#: agentgraph 的重复动作检测会挡下逐字节相同的再执行（只读 SQL 重跑拿回的是
#: 同一份结果，未展示的行不会因此出现）。原来这里复用 _DO_EXEC、靠夹具在第二次
#: 返回另一份数据来模拟"补查成功"，那是现实中不存在的情形。
_DO_EXEC_2 = {"finish": False, "tool": "execute_sql",
              "args": {"sql": "SELECT SUM(gmv) FROM documents"}}


def _patch_exec_script(monkeypatch, script):
    """按顺序返回每次 execute_sql 的结果，其余工具一律成功。"""
    seen = {"i": 0}

    def fake(name, args, ctx):
        if name != "execute_sql":
            return tools.ToolResult(ok=True, tool=name, data={"table": "documents", "columns": []})
        data = script[min(seen["i"], len(script) - 1)]
        seen["i"] += 1
        return tools.ToolResult(ok=True, tool="execute_sql", data=dict(data))

    monkeypatch.setattr(tools, "invoke", fake)


def _run_grounding(tmp_path, monkeypatch, actions, script, mode):
    monkeypatch.setattr(A, "build_quota", lambda c: _Q())
    _patch_recall(monkeypatch)
    _patch_exec_script(monkeypatch, script)
    cfg = _cfg(tmp_path, agent={"grounding": mode, "max_steps": 6, "cost_cap_tokens": 99999})
    return A.run_agent("8 月 GMV 是多少", cfg, 316, executor=_FakeExec(), llm=_FakeLLM(actions))


def test_grounding_shadow_records_but_does_not_block(tmp_path, monkeypatch):
    r = _run_grounding(tmp_path, monkeypatch, [_DO_EXEC, _FAKE_ANSWER], [_PROBE], "shadow")
    assert r.ok and r.ungrounded_numbers            # 记下了，但答案照出
    assert any(s["step"] == "grounding" for s in r.steps)


def test_grounding_enforce_gives_one_chance_to_fix(tmp_path, monkeypatch):
    """先点名回灌让模型去查，补上了就放行 —— 误判的代价只是多跑一轮。"""
    r = _run_grounding(tmp_path, monkeypatch,
                       [_DO_EXEC, _FAKE_ANSWER, _DO_EXEC_2, _GOOD_ANSWER],
                       [_PROBE, _REAL], "enforce")
    assert r.ok and not r.ungrounded_numbers
    assert "7,693,056,921.16" in r.reasoning


def test_grounding_enforce_refuses_when_still_fabricated(tmp_path, monkeypatch):
    r = _run_grounding(tmp_path, monkeypatch,
                       [_DO_EXEC, _FAKE_ANSWER, _FAKE_ANSWER, _FAKE_ANSWER],
                       [_PROBE], "enforce")
    assert not r.ok and r.rejected_by == "UNGROUNDED"
    assert r.rows                                    # 结果表仍要给出来


def test_grounding_never_hurts_a_grounded_answer(tmp_path, monkeypatch):
    r = _run_grounding(tmp_path, monkeypatch, [_DO_EXEC, _GOOD_ANSWER], [_REAL], "enforce")
    assert r.ok and not r.ungrounded_numbers and not r.rejected_by


def test_grounding_off_switch(tmp_path, monkeypatch):
    r = _run_grounding(tmp_path, monkeypatch, [_DO_EXEC, _FAKE_ANSWER], [_PROBE], "off")
    assert r.ok and not r.ungrounded_numbers


def test_history_preview_says_how_many_rows_are_hidden(tmp_path):
    """只写"前几行"太轻，模型会拿看得见的那几行断言整列（L2：18 行里 13 家在用，
    它按 is_active DESC 排序看到开头全 true 就说"18 家全部在用"）。"""
    text = A._render_history([{
        "tool": "execute_sql", "args": {"sql": "SELECT carrier_code, is_active FROM carriers"},
        "brief": "返回 18 行",
        "preview": {"columns": ["carrier_code", "is_active"],
                    "rows": [["AN", True], ["CNSD", True]], "row_count": 18}}])
    assert "仅前 2 行" in text and "共返回 18 行" in text and "不得据此断言整列" in text
