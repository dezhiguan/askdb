"""Evidence ledger helpers used by workers and repair rounds."""

from __future__ import annotations

from typing import Any

from .protocol import Evidence


def latest_by_subtask(items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the active evidence version for every subtask."""
    superseded = {str(item.get("supersedes")) for item in items.values()
                  if item.get("supersedes")}
    active = [item for key, item in items.items() if key not in superseded]
    return {str(item["subtask_id"]): item for item in active}


def from_tool_result(*, run_id: str, subtask_id: str, source_id: str,
                     data: dict[str, Any], bindings: list[dict[str, Any]],
                     attempt: int, supersedes: str = "",
                     scope_fingerprint: str = "",
                     semantic_contract: dict[str, Any] | None = None) -> Evidence:
    from .protocol import SkillBinding

    return Evidence(
        evidence_id=f"{run_id}:evidence:{subtask_id}:{attempt}",
        run_id=run_id,
        task_id=run_id,
        subtask_id=subtask_id,
        source_id=source_id,
        sql_final=str(data.get("sql_final", "")),
        columns=list(data.get("columns", [])),
        rows=list(data.get("rows", [])),
        row_count=int(data.get("row_count", 0)),
        truncated=bool(data.get("truncated", False)),
        as_of=str(data.get("as_of", "")),
        explain_rows=data.get("explain_rows"),
        rules_fired=list(data.get("rules_fired", [])),
        rewrites=list(data.get("rewrites", [])),
        masked_columns=list(data.get("masked_columns", [])),
        mask_degraded=bool(data.get("mask_degraded", False)),
        skill_bindings=[SkillBinding.model_validate(item) for item in bindings],
        supersedes=supersedes,
        scope={"attempt": attempt, "scope_fingerprint": scope_fingerprint,
               "semantic_contract": dict(semantic_contract or {})},
    )
