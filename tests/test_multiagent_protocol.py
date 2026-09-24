from __future__ import annotations

import pytest
from pydantic import ValidationError

from askdb.multiagent.protocol import (
    AgentRole,
    Evidence,
    SubTask,
    TaskPlan,
    single_agent_artifacts,
)


def _task(task_id: str, depends_on: list[str] | None = None) -> SubTask:
    return SubTask(
        subtask_id=task_id,
        title=task_id,
        assigned_role=AgentRole.QUERY_WORKER,
        depends_on=depends_on or [],
    )


def test_task_plan_rejects_unknown_dependency_and_cycle():
    with pytest.raises(ValidationError, match="unknown tasks"):
        TaskPlan(plan_id="p", run_id="r", question="q",
                 subtasks=[_task("a", ["missing"])])

    with pytest.raises(ValidationError, match="cycle"):
        TaskPlan(plan_id="p", run_id="r", question="q",
                 subtasks=[_task("a", ["b"]), _task("b", ["a"])])


def test_evidence_checksum_is_stable_and_content_sensitive():
    common = dict(
        evidence_id="e", run_id="r", task_id="t", subtask_id="s",
        source_id="db", sql_final="SELECT 1", columns=["n"], rows=[[1]],
        row_count=1,
    )
    first = Evidence(**common)
    same = Evidence(**common)
    changed = Evidence(**{**common, "rows": [[2]]})

    assert first.checksum.startswith("sha256:")
    assert first.checksum == same.checksum
    assert first.checksum != changed.checksum


def test_single_agent_adapter_keeps_every_successful_execution():
    artifacts = single_agent_artifacts(
        question="比较本月与上月", trace_id="trace", source_id="main",
        exec_results=[
            {"sql_final": "SELECT 1", "columns": ["n"], "rows": [[1]], "row_count": 1},
            {"sql_final": "SELECT 2", "columns": ["n"], "rows": [[2]], "row_count": 1},
        ],
        answer="本月增加。",
    )

    assert artifacts.execution_mode == "single"
    assert artifacts.plan.subtasks[0].status == "SUCCEEDED"
    assert len(artifacts.evidence) == 2
    assert artifacts.reviews[0].verdict == "PASS"
    assert artifacts.claims[0].evidence_ids == [
        "trace:evidence:0", "trace:evidence:1"]


def test_single_agent_adapter_does_not_invent_evidence():
    artifacts = single_agent_artifacts(
        question="不能回答的问题", trace_id="trace", source_id="main",
        exec_results=[], answer="当前无法回答。",
    )

    assert artifacts.evidence == []
    assert artifacts.reviews == []
    assert artifacts.claims[0].confidence == 0.25
    assert artifacts.claims[0].caveats
