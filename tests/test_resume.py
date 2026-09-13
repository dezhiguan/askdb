"""任务中断恢复：中断兜底、断点续跑、审计关联、统一 404。

2026-09-12 整篇从老管道重写到 agent 图（askdb/agentgraph）。

**这些用例此前验的是一条没人走的路。** 检查点续跑一直挂在老管道上，而线上
所有真实请求走的是 agent 循环（没有检查点）—— 近 30 天 interrupted=0、
recovered=0，也就是说"断点续跑"这个能力在生产上从未触发过一次。agent 改用
LangGraph 之后检查点第一次覆盖到真正在跑的那条链路，这些用例才开始有意义。

判定本身一条没松：中断要留下可续的现场、续跑要写新 trace 同 thread、
表被收回 / 结构漂移必须挡住、被挡之后仍然可续。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from askdb import agentgraph, server, tools
from askdb.audit import get_audit, read_records

OK_SQL = "SELECT COUNT(*) AS n FROM documents"


class FakeLlm:
    """按脚本依次返回决策；不配脚本就一直"还没想好"（停不下来）。

    真 LlmClient 的 model_name 是 property，这里保持一致 —— 当成方法调过一次，
    线上直接 500（见 aae409b）。
    """

    def __init__(self, *actions, answerable=True):
        self.answerable = answerable
        self.actions = list(actions) or [_act()]
        self.calls: list[str] = []
        self.i = 0

    @property
    def model_name(self) -> str:
        return "fake"

    def structured(self, schema, system, human):
        u = SimpleNamespace(input_tokens=10, output_tokens=2, cost_cny=0.0)
        if schema.__name__ == "IntentCheck":
            return SimpleNamespace(answerable=self.answerable, out_of_scope=False,
                                   reason="ok", clarify="补充一下"), u
        self.calls.append(human)
        a = self.actions[min(self.i, len(self.actions) - 1)]
        self.i += 1
        return a, u


def _act(finish=False, answer="", tool="execute_sql", sql=OK_SQL):
    return SimpleNamespace(finish=finish, answer=answer, tool=tool,
                           thought="想一下", args={"sql": sql})


@pytest.fixture(autouse=True)
def _fresh_graph():
    """每个用例拿一张新编译的图 —— 缓存跨用例复用会带着上一个 saver。"""
    agentgraph.reset_graph()
    yield
    agentgraph.reset_graph()


def _run(cfg, ex, llm, question="有多少文档", thread_id=None):
    from askdb.agent import run_agent

    return run_agent(question, cfg, executor=ex, llm=llm, thread_id=thread_id)


def _interrupted(cfg, ex, monkeypatch):
    """让工具调用抛进程级异常，制造一次真实中断。"""
    with monkeypatch.context() as m:
        m.setattr(tools, "invoke",
                  lambda *a, **k: (_ for _ in ()).throw(RuntimeError("进程被杀")))
        return _run(cfg, ex, FakeLlm(_act()))


# ---------------------------------------------------------------- 中断

def test_interrupt_leaves_trace_and_resumable_checkpoint(cfg, ex, monkeypatch):
    """中断必须留下两样东西：一条能查的审计，和一个能续的现场。

    少了审计，任务中心整片列不出这条线程（2026-09-07 实测过）；
    少了现场，"可续跑"这一档就永远是空的。
    """
    r1 = _interrupted(cfg, ex, monkeypatch)
    assert r1.ok is False
    assert r1.trace_id and r1.thread_id == r1.trace_id     # 客户端由此持有续跑凭据
    rec = get_audit(cfg.audit_log, r1.trace_id)
    assert rec is not None and rec["kind"] == "ask"
    # 检查点停在中断节点之前，线程没走完
    assert agentgraph.is_resumable(r1.thread_id, cfg) is True


def test_resume_completes_and_links_audit(cfg, ex, monkeypatch):
    """续跑写新 trace、同一个 thread —— 审计里看得出"这是第 2 次执行"。"""
    r1 = _interrupted(cfg, ex, monkeypatch)
    r2 = agentgraph.resume(r1.thread_id, cfg, executor=ex,
                           llm=FakeLlm(_act(), _act(finish=True, answer="有数据")))
    assert r2 is not None and r2.ok
    assert r2.trace_id != r1.trace_id
    assert r2.thread_id == r1.thread_id
    kinds = [x["kind"] for x in read_records(cfg.audit_log)]
    assert kinds[-1] == "resume", kinds


def test_resume_missing_or_finished_returns_none(cfg, ex):
    """不存在、已跑完 —— 都返回 None，由接口层与"不存在"同样处理。

    **已跑完那条尤其要挡**：不挡的话，一条线程可以被无限次重放，每次都记成
    "第 N 次执行"，而其实什么新输入都没有。
    """
    assert agentgraph.resume("0123456789ab", cfg, executor=ex) is None
    done = _run(cfg, ex, FakeLlm(_act(), _act(finish=True, answer="好")))
    assert done.ok
    assert agentgraph.resume(done.thread_id, cfg, executor=ex) is None


# ---------------------------------------------------------------- 补充

def test_clarification_reaches_the_model_without_rewriting_the_question(cfg, ex):
    """补充条件要真的喂进决策，而且**不改写原问题**。

    回归的是这个 bug：补充被丢掉之后，补多少次都还是同一句"信息不足"，
    出口形同虚设。而 question 一旦被就地改写，同一条线程在界面上会变成
    另一个问题 —— 审计标题与审批指纹都读它。
    """
    llm = FakeLlm(_act(finish=True, answer="好"))
    r = agentgraph.resume("0123456789ab", cfg, executor=ex, llm=llm,
                          question="帮我分析一下",
                          clarification="查 documents 表，按 status 分组")
    assert r is not None and r.question == "帮我分析一下"   # 原问题没被改写
    assert any("documents" in c for c in llm.calls), "补充条件没进决策提示词"


def test_resume_without_new_input_is_not_a_rerun(cfg, ex):
    """没有活现场、又没给问题原文 —— 不跑，返回 None。

    不带任何新信息再跑一遍，拿到的必然还是同一个结果，只是白花一次配额。
    """
    assert agentgraph.resume("0123456789ab", cfg, executor=ex) is None


# ---------------------------------------------------------------- 续跑前校验

def test_resume_blocked_when_table_no_longer_visible(cfg, ex, monkeypatch):
    """中断期间表被收回：不能拿旧前提接着跑。"""
    r1 = _interrupted(cfg, ex, monkeypatch)
    narrowed = _without_tables(cfg)
    r2 = agentgraph.resume(r1.thread_id, narrowed, executor=ex, llm=FakeLlm())
    assert r2 is not None and r2.ok is False
    assert r2.rejected_by == "RESUME_BLOCKED"


def test_blocked_resume_keeps_the_task_resumable(cfg, ex, monkeypatch):
    """被前置校验挡下**不是终态**：现场还在，条件恢复后照样能续。

    归成普通拒绝的话，任务中心会把它当"已收尾"，续跑入口跟着消失 ——
    而它恰恰是唯一一档"等条件恢复"的任务。
    """
    r1 = _interrupted(cfg, ex, monkeypatch)
    agentgraph.resume(r1.thread_id, _without_tables(cfg), executor=ex, llm=FakeLlm())
    assert agentgraph.is_resumable(r1.thread_id, cfg) is True


def test_blocked_resume_is_audited(cfg, ex, monkeypatch):
    """挡下来这件事本身要留痕，否则"为什么没跑"事后说不清。"""
    r1 = _interrupted(cfg, ex, monkeypatch)
    r2 = agentgraph.resume(r1.thread_id, _without_tables(cfg), executor=ex, llm=FakeLlm())
    rec = get_audit(cfg.audit_log, r2.trace_id)
    assert rec is not None and rec["rejected_by"] == "RESUME_BLOCKED"


def _without_tables(cfg):
    """一份把可见表清空的配置 —— 模拟中断期间权限被收窄。"""
    import copy
    import dataclasses

    return dataclasses.replace(cfg, tables={}, raw=copy.deepcopy(cfg.raw))


# ---------------------------------------------------------------- 接口

def test_resume_endpoint_uniform_404(cfg, monkeypatch):
    """非法、不存在、别人的 —— 三种响应**逐字节一致**。

    区分就等于给了一个探测"某条线程存不存在"的入口，而线程里带着别人问过的
    问题原文。
    """
    monkeypatch.setattr(server, "load", lambda _p: cfg)
    client = TestClient(server.create_app("ignored.yaml"))

    bodies = set()
    for tid in ("../etc", "ABCDEF123456", "0123456789ab"):
        resp = client.post("/api/resume", json={"thread_id": tid})
        assert resp.status_code == 404
        bodies.add(resp.text)
    assert len(bodies) == 1


# ---------------------------------------------------------------- 复放与降级

def test_replay_returns_the_decision_trail(cfg, ex, monkeypatch):
    """复放取回的是**决策轨迹**，不是 SQL 全文。

    agent 的每一步快照里有意义的是"第几步、挑了哪个工具、有没有结论"——
    失败复现要看的就是它在哪一步拐错了弯。SQL 全文另有 replay_api 那道开关
    管着（对外实例默认关），这里一个字都不出。
    """
    r1 = _interrupted(cfg, ex, monkeypatch)
    snaps = agentgraph.replay(r1.thread_id, cfg)
    assert snaps, "中断的线程没有任何快照"
    assert all({"next", "step", "tool"} <= set(s) for s in snaps)
    assert not any("sql" in str(k).lower() for s in snaps for k in s), \
        "复放把 SQL 泄出来了"


def test_is_resumable_degrades_to_none_not_false(cfg, monkeypatch):
    """检查点库问不通时返回 None，**不是 False**。

    None = "不知道"，False = "确定不能续"。压成 False 的话，库抖一下就会把
    一批还能救的任务标成不可续 —— 而任务中心据此把入口灰掉，人就再也点不到了。
    """
    def _boom(_cfg):
        raise RuntimeError("检查点库连不上")

    monkeypatch.setattr(agentgraph, "ensure_graph", _boom)
    assert agentgraph.is_resumable("a" * 12, cfg) is None


def test_clarification_is_written_back_into_the_live_checkpoint(cfg, ex, monkeypatch):
    """**真中断 + 补充条件**：补充要写回检查点，模型续跑时才看得到。

    这条与"没有活现场的重跑"是两条不同的路，容易只测到其中一条：
      · 无检查点 → 带补充重跑整条链路（initial_state 把它放进 history）
      · 有检查点 → 恢复是从图**内部**继续的，中间节点拿不到 resume() 的入参，
        只看得到状态 —— 所以必须 update_state 写回去。

    漏掉这一步的症状很隐蔽：续跑成功、也没报错，只是补充那句话凭空消失，
    模型照着原来的历史又跑一遍。
    """
    r1 = _interrupted(cfg, ex, monkeypatch)
    assert agentgraph.is_resumable(r1.thread_id, cfg) is True

    llm = FakeLlm(_act(finish=True, answer="好了"))
    r2 = agentgraph.resume(r1.thread_id, cfg, executor=ex, llm=llm,
                           clarification="只算 status='FAILED' 的")
    assert r2 is not None
    assert any("FAILED" in c for c in llm.calls), "补充没写回检查点，模型看不到"


def test_resume_survives_a_checkpoint_that_refuses_writes(cfg, ex, monkeypatch):
    """补充写不回去时**照样能续** —— 那是原有语义，不能因为加了补充就更脆。

    检查点库抖一下就让续跑整个失败，等于把一个可选增强变成了新的单点。
    """
    r1 = _interrupted(cfg, ex, monkeypatch)
    g = agentgraph.ensure_graph(cfg)
    monkeypatch.setattr(g, "update_state",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("写不进去")))
    r2 = agentgraph.resume(r1.thread_id, cfg, executor=ex,
                           llm=FakeLlm(_act(finish=True, answer="好了")),
                           clarification="只算失败的")
    assert r2 is not None and r2.ok, "补充写失败把续跑也带崩了"
