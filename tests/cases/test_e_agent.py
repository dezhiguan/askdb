"""E 域 · agent 图的路由、循环上限与护栏（R-16 / R-17）。

2026-09-12 取代 test_e_graph.py。那一份验的是固定管道的重试/反思路由，
管道当天随 agent 迁到 LangGraph 一并删除（见 askdb/graph.py 顶部说明）。

**只搬仍然成立的不变量**，一条都没放松：
  · R-16 步数上限 —— 触顶要收敛作答并**显式标注**，不能静默截断
  · R-17 token 上限 —— 同上
  · 每一次工具调用独立过闸 —— 不存在"上一步过了所以这步可信"的通道
  · 没取到数就不许出数字（P0 兜底）

管道特有的那些（reflect 重写同一条 SQL、attempt 逐步重置）没有对应物：
agent 不重写，它换工具换方向。没有对应物的就不搬，**不造一个形似的替身**。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from askdb import agentgraph, tools
from askdb.trace import Tracer

OK_SQL = "SELECT COUNT(*) AS n FROM documents"


class FakeLlm:
    def __init__(self, *actions, answerable=True):
        self.answerable = answerable
        self.actions = list(actions) or [_act()]
        self.i = 0

    @property
    def model_name(self) -> str:
        return "fake"

    def structured(self, schema, system, human):
        u = SimpleNamespace(input_tokens=500, output_tokens=50, cost_cny=0.0)
        if schema.__name__ == "IntentCheck":
            return SimpleNamespace(answerable=self.answerable, out_of_scope=False,
                                   reason="ok", clarify=""), u
        a = self.actions[min(self.i, len(self.actions) - 1)]
        self.i += 1
        return a, u


def _act(finish=False, answer="", tool="execute_sql", sql=OK_SQL):
    return SimpleNamespace(finish=finish, answer=answer, tool=tool,
                           thought="想一下", args={"sql": sql})


def _rows(n=1):
    return {"columns": ["n"], "rows": [[n]], "row_count": 1, "sql_final": OK_SQL}


@pytest.fixture
def run(cfg, monkeypatch):
    """跑一遍 agent 图，返回 (AskResult, 工具调用名列表)。"""
    def _go(llm, tool_result, max_steps=6, cost_cap=32000):
        calls: list[str] = []

        def _invoke(name, args, ctx):
            calls.append(name)
            return tool_result

        monkeypatch.setattr(tools, "invoke", _invoke)
        monkeypatch.setattr(tools, "search_schema", lambda q, c: tools.ToolResult(
            ok=True, tool="search_schema", data={"tables": ["documents"], "prompt": "表"}))
        tracer = Tracer()
        deps = agentgraph.Deps(cfg=cfg, llm=llm, executor=None, tracer=tracer,
                               ctx=SimpleNamespace(last_result=None))
        init = agentgraph.initial_state("q", 0, "a" * 12, "b" * 12, max_steps, cost_cap)
        final = agentgraph.build_skeleton().compile().invoke(
            init, {"configurable": {"deps": deps},
                   "recursion_limit": agentgraph.recursion_limit(max_steps)})
        return agentgraph.to_result(final, cfg, tracer), calls
    return _go


def test_e16_step_cap_converges_and_says_so(run):
    """R-16 步数上限：触顶即收敛作答，并**显式标注**不完整。

    静默截断比截断本身危险：页面上它与一个跑完的结果长得一模一样。
    """
    r, calls = run(FakeLlm(*[_act()] * 10), tools.ToolResult(
        ok=True, tool="execute_sql", data=_rows()), max_steps=2)
    assert r.converged_early, "触顶必须显式标注，不能静默收敛"
    assert "步数" in r.converged_early
    assert len(calls) <= 2, f"超了步数上限还在调工具：{calls}"


def test_e17_token_cap_converges_and_says_so(run):
    """R-17 累计 token 上限：同上。

    **预算必须读 state 里的 tok_used**，不能读 tracer —— 续跑一次 tracer 就是
    空的，预算跟着归零，等于没有上限。
    """
    r, _ = run(FakeLlm(*[_act()] * 10), tools.ToolResult(
        ok=True, tool="execute_sql", data=_rows()), cost_cap=1)
    assert r.converged_early
    assert "token" in r.converged_early.lower() or "预算" in r.converged_early


def test_e18_every_tool_call_passes_its_own_gate(run):
    """每一次工具调用独立过闸，不存在"已被信任"的通道。

    护栏在 tools.execute_sql **内部**（AST → 干跑 → 只读执行 → 脱敏），
    所以第二次调用照样从头过一遍 —— 这条钉的是"拦下就该拦下"，
    不因为是第 N 次调用而放行。
    """
    blocked = tools.ToolResult(ok=False, tool="execute_sql", rejected_by="R-03",
                               error="不可查的表", data={})
    r, calls = run(FakeLlm(*[_act()] * 5), blocked, max_steps=3)
    assert not r.ok
    assert r.sql_final == "", "被拦下的 SQL 不该出现在结果里"


def test_e19_no_data_means_no_numbers(run):
    """P0 兜底：本轮一次都没执行成功，就不许给出带数字的结论。

    2026-09-11 跑测 130 条里 15 条是"一次 SQL 都没发、直接写个整数"，
    数量级差 2~3 个，界面上无从分辨。
    """
    failed = tools.ToolResult(ok=False, tool="execute_sql", error="语法错", data={})
    r, _ = run(FakeLlm(_act(), _act(finish=True, answer="一共 123456 条")), failed)
    assert r.rejected_by == "NO_EVIDENCE"
    assert "123456" not in (r.reasoning or ""), "编造的数字被回显了"
