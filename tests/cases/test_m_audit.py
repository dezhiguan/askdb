"""M 域 · 可观测、审计与配额（6 条）。"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
import pytest
from askdb import graph
from askdb.llm import LlmUsage, SqlDraft
from askdb.trace import today_calls

ROOT = Path(__file__).resolve().parent.parent.parent
OK_SQL = "SELECT file_name AS 文件名 FROM documents WHERE status = 'PROCESSING'"


class FakeLlm:
    def __init__(self, *s): self.sqls = list(s)
    def generate_sql(self, *a, **k):
        return SqlDraft(sql=self.sqls.pop(0) if self.sqls else "", reasoning="t"), LlmUsage(10, 5)


def test_m01_audit_fields_complete(cfg, ex):
    graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    rec = json.loads(Path(cfg.audit_log).read_text(encoding="utf-8").splitlines()[-1])
    want = {"trace_id", "ts", "org_id", "question", "tables_hit", "metrics_hit",
            "sql_raw", "sql_final", "rules_fired", "rejected_by", "attempts",
            "explain_rows", "rows_returned", "steps", "cost_cny"}
    assert not (want - set(rec)), f"缺字段：{want - set(rec)}"


def test_m02_rejected_calls_are_audited(cfg, ex):
    graph.ask("q", cfg, executor=ex, llm=FakeLlm("DELETE FROM documents"))
    rec = json.loads(Path(cfg.audit_log).read_text(encoding="utf-8").splitlines()[-1])
    assert rec["rejected_by"], "被拒调用同样要留痕"


def test_m03_cost_attributed_per_step(cfg, ex):
    r = graph.ask("q", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert any(s.get("tok_in") for s in r.steps), "成本必须归因到步骤"


def test_m04_daily_quota_blocks(cfg, ex):
    cfg.raw["observability"]["daily_quota"] = 1
    graph.ask("q1", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    r = graph.ask("q2", cfg, executor=ex, llm=FakeLlm(OK_SQL))
    assert not r.ok and ("配额" in (r.error or "") or r.rejected_by == "QUOTA")


def test_m05_quota_counts_today_only(cfg, tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"ts": "2020-01-01T00:00:00+08:00"}\n', encoding="utf-8")
    assert today_calls(p) == 0, "跨日应归零"


def test_m06_audit_and_checkpoints_not_tracked():
    out = subprocess.run(["git", "ls-files", "data/"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    leaked = [x for x in out.splitlines()
              if "audit" in x or "checkpoint" in x]
    assert not leaked, f"审计/检查点不得入库：{leaked}"
