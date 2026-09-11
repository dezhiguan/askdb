"""可信数据 Agent 运行时（tools / skill / agent 循环 / 异步）单元测试。

全部自足：合成 Config + 打桩 LLM/执行器，不连真实库或模型。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from askdb import agent as A
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
