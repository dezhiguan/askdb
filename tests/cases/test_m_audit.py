"""M 域 · 可观测、审计与配额（6 条）。"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
import pytest
from types import SimpleNamespace

from askdb import tools
from askdb.agent import run_agent
from askdb.llm import LlmUsage

ROOT = Path(__file__).resolve().parent.parent.parent
OK_SQL = "SELECT file_name AS 文件名 FROM documents WHERE status = 'PROCESSING'"


# 2026-09-12 从打桩 graph.ask 改到 agent：固定管道当天删除。
# **这四条验的都与链路无关**（审计字段完整、被拒留痕、成本归因到步骤、
# 配额拦截），所以换掉的只是驱动方式，判据一字没动。
class FakeLlm:
    """按脚本依次决策。model_name 是 property —— 真 LlmClient 也是，
    当成方法调过一次线上直接 500（aae409b）。"""

    def __init__(self, *sqls):
        self.sqls = list(sqls)
        self.i = 0

    @property
    def model_name(self) -> str:
        return "fake"

    def structured(self, schema, system, human):
        u = LlmUsage(10, 5)
        if schema.__name__ == "IntentCheck":
            return SimpleNamespace(answerable=True, out_of_scope=False,
                                   reason="ok", clarify=""), u
        if self.i < len(self.sqls):
            sql = self.sqls[self.i]
            self.i += 1
            return SimpleNamespace(finish=False, answer="", tool="execute_sql",
                                   thought="查一下", args={"sql": sql}), u
        return SimpleNamespace(finish=True, answer="好了", tool="",
                               thought="够了", args={}), u


def _ask(cfg, ex, llm, q="q"):
    return run_agent(q, cfg, executor=ex, llm=llm)


def test_m01_audit_fields_complete(cfg, ex):
    _ask(cfg, ex, FakeLlm(OK_SQL))
    rec = json.loads(Path(cfg.audit_log).read_text(encoding="utf-8").splitlines()[-1])
    want = {"trace_id", "ts", "org_id", "question", "tables_hit", "metrics_hit",
            "sql_raw", "sql_final", "rules_fired", "rejected_by", "attempts",
            "explain_rows", "rows_returned", "steps", "cost_cny"}
    assert not (want - set(rec)), f"缺字段：{want - set(rec)}"


def test_m02_rejected_calls_are_audited(cfg, ex):
    """被护栏拦下的那次调用必须留痕。

    2026-09-12 判据从 rejected_by 挪到 span：**agent 下这两件事分开了**。
    管道里一条 SQL 被拦 = 整次请求失败，所以看顶层 rejected_by 就够；
    agent 被拦之后可以换个工具换个方向接着跑，整次请求未必失败 ——
    这时顶层是 null，而"有一次调用被拦下"这个事实在 span 里。

    要钉的是"拦下来这件事没有消失"，不是"整次请求必须失败"。
    """
    _ask(cfg, ex, FakeLlm("DELETE FROM documents"))
    rec = json.loads(Path(cfg.audit_log).read_text(encoding="utf-8").splitlines()[-1])
    blocked = [s for s in rec.get("steps", []) if s.get("status") == "blocked"]
    assert blocked, f"被拒调用没有留痕：{rec.get('steps')}"


def test_m03_cost_attributed_per_step(cfg, ex):
    r = _ask(cfg, ex, FakeLlm(OK_SQL))
    assert any(s.get("tok_in") for s in r.steps), "成本必须归因到步骤"


def test_m04_daily_quota_blocks(cfg, ex):
    from askdb.quota import build_quota

    cfg.raw["observability"]["daily_quota"] = 1
    build_quota(cfg).reserve()
    r = _ask(cfg, ex, FakeLlm(OK_SQL), q="q2")
    assert not r.ok and ("上限" in (r.error or "") or r.rejected_by == "QUOTA")


def test_m05_quota_counts_today_only(cfg, tmp_path):
    """计数按日归零。改日期即换键，昨天用满不影响今天。"""
    import json

    from askdb import quota
    from askdb.quota import build_quota

    cfg.raw["observability"]["daily_quota"] = 5
    dq = build_quota(cfg)
    dq.reserve()
    assert dq.peek() == 1
    # 把计数文件改成昨天的记录 —— 等价于跨了一天
    dq.backend.path.write_text(json.dumps({"date": "2020-01-01", "used": 5}),
                               encoding="utf-8")
    assert dq.peek() == 0, "跨日应归零"
    assert quota.build_quota(cfg).reserve() == 1


def test_m06_audit_and_checkpoints_not_tracked():
    out = subprocess.run(["git", "ls-files", "data/"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    leaked = [x for x in out.splitlines()
              if "audit" in x or "checkpoint" in x]
    assert not leaked, f"审计/检查点不得入库：{leaked}"
