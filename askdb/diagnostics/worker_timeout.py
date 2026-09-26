"""Exercise a real Worker and R-12 timeout against the image's sample DuckDB.

Run inside an askdb Pod: python -m askdb.diagnostics.worker_timeout
No production datasource credentials or model calls are used.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

from .. import tools
from ..config import load
from ..executor import DataSourceError, Executor
from ..multiagent.query_worker import run_worker
from ..multiagent.state import initial_state
from ..multiagent.supervisor_graph import MultiAgentDeps
from ..trace import Tracer


SQL = "SELECT SUM(range) FROM range(400000000)"


class _ProbeLlm:
    def generate_sql(self, *_args, **_kwargs):
        return SimpleNamespace(sql=SQL), SimpleNamespace(
            input_tokens=0, output_tokens=0, cost_cny=0.0)


def probe(db_path: str | Path = "data/sample.duckdb") -> dict:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"隔离样例库不存在：{path}")
    cfg = load("config/askdb.yaml")
    cfg.raw["datasource"] = {"type": "duckdb", "path": str(path), "read_only": True}
    cfg.raw["guard"] = {**cfg.raw["guard"], "statement_timeout_ms": 150}
    cfg.source_id = "diagnostic_duckdb"
    deps = MultiAgentDeps(cfg=cfg, llm=_ProbeLlm(), tracer=Tracer(),
                          llm_factory=lambda _cfg: _ProbeLlm(),
                          executor_factory=lambda source_cfg: Executor(source_cfg))
    state = initial_state(
        question="验证隔离数据源上的 Worker 超时", run_id="timeout_probe",
        thread_id="timeout_probe", org_id=65, source_id=cfg.source_id)
    state["worker_task"] = {
        "subtask_id": "timeout_probe:worker:0", "title": "R-12 超时探针",
        "source_id": cfg.source_id, "status": "PENDING", "attempt": 0,
        "query": {"question": "执行固定的隔离超时探针"},
    }
    original_search, original_execute = tools.search_schema, tools.execute_sql

    def schema(_question, _cfg):
        return tools.ToolResult(ok=True, tool="search_schema",
                                data={"prompt": "range(i) 虚拟表", "tables": []})

    def execute(sql, source_cfg, _org_id, executor):
        if sql != SQL or source_cfg is not cfg:
            return tools.ToolResult(ok=False, tool="execute_sql",
                                    error="探针只允许固定 SQL 和隔离数据源")
        try:
            executor.run(sql)
        except DataSourceError as exc:
            return tools.ToolResult(ok=False, tool="execute_sql",
                                    rejected_by="R-12" if "超时" in str(exc) else "EXEC",
                                    error=str(exc))
        return tools.ToolResult(ok=True, tool="execute_sql", data={
            "sql_final": sql, "columns": ["sum"], "rows": [[0]], "row_count": 1})

    started = time.monotonic()
    try:
        tools.search_schema, tools.execute_sql = schema, execute
        out = run_worker(state, deps)
    finally:
        tools.search_schema, tools.execute_sql = original_search, original_execute
    task = out["subtasks_by_id"]["timeout_probe:worker:0"]
    return {
        "ok": task["status"] == "FAILED" and "R-12" in task["error"],
        "status": task["status"], "error": task["error"],
        "source": "isolated_duckdb", "timeout_ms": 150,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/sample.duckdb")
    args = parser.parse_args()
    report = probe(args.db)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] and report["elapsed_ms"] < 5000 else 1


if __name__ == "__main__":
    raise SystemExit(main())
