"""收尾码 → 任务态的映射必须是**满的**。

2026-09-12 agent 迁到 LangGraph、固定管道删除之后，收尾码整套换了人：
管道产 NO_SQL / EXEC，agent 产 CLARIFY / DATASOURCE / OOS / UNGROUNDED /
NO_EVIDENCE / NO_RESULT。而 audit.stage 当时只认管道那两个 —— 后果不是报错，
是**两档人工介入静默失效**：

  · 问得不够具体（CLARIFY）掉进「已拦截」，界面对他说"这条触碰的是安全边界，
    改写法也过不去"—— 而他补一句就能跑通；
  · 库连不上（DATASOURCE）同样掉进「已拦截」，永远进不了运维队列。

更隐蔽的是「等待补充」会**恒为空**：NO_SQL 再也不出现，存量老化出窗口之后
那一档归零，看起来像"问题变少了"。

所以这个文件钉的不是某一条映射，而是**这张表有没有缺口**：agent 里每一个
会写进审计的 rejected_by，都必须能折算出一个有意义的任务态。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from askdb import audit

ROOT = Path(__file__).resolve().parent.parent


def _codes_the_agent_emits() -> set[str]:
    """扫出 agent 链路实际会落进审计的 rejected_by。

    正则取的是 `"rejected_by": "XXX"` 与 `rejected_by="XXX"` 两种写法 ——
    两处都有，漏一种就等于这条用例只看了一半。
    """
    codes: set[str] = set()
    for name in ("agentgraph.py", "agent.py"):
        src = (ROOT / "askdb" / name).read_text(encoding="utf-8")
        codes |= set(re.findall(r'"rejected_by":\s*"([A-Z_0-9-]+)"', src))
        codes |= set(re.findall(r'rejected_by="([A-Z_0-9-]+)"', src))
    return codes


def test_every_agent_reject_code_maps_to_a_state():
    """每个收尾码都折算得出任务态，且**不是清一色的「已拦截」**。

    全落 REJECTED 在代码上永远"通过"（那是兜底分支），所以这条用例同时要求
    该分流的确实分流了 —— 否则它只是一句恒真的断言。
    """
    codes = _codes_the_agent_emits()
    assert len(codes) >= 6, f"没扫到几个收尾码，正则或 agent 结构变了：{codes}"

    got = {c: audit.stage({"rejected_by": c}) for c in codes}
    for code, st in got.items():
        assert st in audit.TASK_STATUSES, f"{code} 折算出未知任务态 {st}"

    # 这三条是分流的全部意义：下一步该谁动手完全不同
    assert got.get("CLARIFY") == audit.WAITING_INPUT, (
        "问得不够具体被判成已拦截 —— 补一句就能跑通的人会被告知'改写法也过不去'")
    assert got.get("DATASOURCE") == audit.NEEDS_OPERATOR, (
        "库连不上被判成已拦截 —— 它进不了运维队列，也没人会去看")
    assert got.get("OOS") == audit.REJECTED, (
        "越域不该进等待补充：库里没有这种数据，补充再多也没用")


@pytest.mark.parametrize("code,want", [
    ("EXEC", audit.NEEDS_OPERATOR),
    ("DATASOURCE", audit.NEEDS_OPERATOR),
    ("NO_SQL", audit.WAITING_INPUT),
    ("CLARIFY", audit.WAITING_INPUT),
    ("R-03", audit.REJECTED),
    ("UNGROUNDED", audit.REJECTED),
    ("NO_EVIDENCE", audit.REJECTED),
])
def test_code_lands_in_the_right_bucket(code, want):
    """逐条钉住。**NO_SQL 与 EXEC 要留着** —— 管道虽然删了，历史审计里
    还有大量这两种记录，任务中心至今在列它们。"""
    assert audit.stage({"rejected_by": code}) == want


def test_ops_codes_leave_the_queue_once_handled(code_free=None):
    """运维处置过的，两种收尾码都要离开队列 —— 否则标了也白标。"""
    for code in ("EXEC", "DATASOURCE"):
        assert audit.stage({"rejected_by": code}, ops_status="RESOLVED") == audit.REJECTED
        assert audit.stage({"rejected_by": code}, ops_status="WONTFIX") == audit.REJECTED


def test_every_state_tells_you_who_acts_next():
    """每一档都要说得出"下一步该谁动手"——那是分档的全部意义。"""
    for st in audit.TASK_STATUSES:
        if st == audit.DONE:
            continue
        assert audit._NEXT_ACTOR.get(st), f"{st} 没有 next_actor，页面无话可说"
