"""Deterministic four-layer comparison of a single answer and its shadow run."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any


_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w.])")
_MAGNITUDE = re.compile(r"(?:约|大约|近|相差)?([一二三四五六七八九十\d]+)个数量级")
_QUOTED_SQL = re.compile(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")")


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     default=str, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _number(value: Any) -> str:
    text = str(value).replace(",", "")
    try:
        return format(Decimal(text).normalize(), "f")
    except InvalidOperation:
        return str(value).strip()


def _rows(result: Any) -> list[str]:
    evidence = result.evidence or []
    groups = [row.get("rows") or [] for row in evidence] if evidence else [result.rows]
    return sorted(_number(cell) for group in groups for row in group for cell in row)


def _row_structure(result: Any) -> list[dict[str, Any]]:
    evidence = result.evidence or []
    groups = evidence if evidence else [{
        "columns": result.columns, "rows": result.rows}]
    return sorted(({
        "columns": [str(col).strip().lower() for col in group.get("columns") or []],
        "rows": sorted([_number(cell) for cell in row]
                       for row in group.get("rows") or []),
    } for group in groups), key=lambda group: _fingerprint(group))


def _sql(result: Any) -> list[str]:
    evidence = result.evidence or []
    statements = [row.get("sql_final", "") for row in evidence] if evidence else [result.sql_final]
    normalized = []
    for sql in statements:
        if not sql:
            continue
        parts = _QUOTED_SQL.split(str(sql).strip())
        normalized.append("".join(
            part if index % 2 else re.sub(r"\s+", " ", part).lower()
            for index, part in enumerate(parts)))
    return sorted(normalized)


def _facts(answer: str) -> list[str]:
    return sorted(_number(match.group()) for match in _NUM.finditer(answer))


def _evidence(result: Any) -> dict[str, Any]:
    ids = {str(item.get("evidence_id")) for item in result.evidence or []}
    claims = result.claims or []
    unbound = sum(not claim.get("evidence_ids") or not set(
        claim["evidence_ids"]).issubset(ids) for claim in claims)
    return {
        "count": len(ids),
        "with_checksum": sum(bool(item.get("checksum")) for item in result.evidence or []),
        "unbound_claims": unbound,
        "verified": any(review.get("verdict") == "PASS" for review in result.reviews or []),
    }


def compare(single: Any, shadow: Any) -> dict[str, Any]:
    """Return bounded audit metadata; never copy result rows into the comparison."""
    baseline_sql, shadow_sql = _sql(single), _sql(shadow)
    baseline_rows, shadow_rows = _rows(single), _rows(shadow)
    baseline_structure, shadow_structure = _row_structure(single), _row_structure(shadow)
    baseline_answer = str(single.reasoning or "")
    shadow_answer = str(shadow.reasoning or "")
    baseline_facts, shadow_facts = _facts(baseline_answer), _facts(shadow_answer)
    baseline_magnitude = sorted(_MAGNITUDE.findall(baseline_answer))
    shadow_magnitude = sorted(_MAGNITUDE.findall(shadow_answer))
    layers = {
        "sql": {
            "same": baseline_sql == shadow_sql,
            "single_count": len(baseline_sql), "shadow_count": len(shadow_sql),
            "single_hash": _fingerprint(baseline_sql),
            "shadow_hash": _fingerprint(shadow_sql),
        },
        "data": {
            "same": baseline_rows == shadow_rows
                    and baseline_structure == shadow_structure,
            "values_same": baseline_rows == shadow_rows,
            "structure_same": baseline_structure == shadow_structure,
            "single_values": len(baseline_rows), "shadow_values": len(shadow_rows),
            "single_hash": _fingerprint(baseline_rows),
            "shadow_hash": _fingerprint(shadow_rows),
            "single_structure_hash": _fingerprint(baseline_structure),
            "shadow_structure_hash": _fingerprint(shadow_structure),
        },
        "conclusion": {
            "same": bool(baseline_answer and shadow_answer)
                    and re.sub(r"\s+", "", baseline_answer)
                    == re.sub(r"\s+", "", shadow_answer),
            "numeric_facts_same": baseline_facts == shadow_facts,
            "magnitude_claims_same": baseline_magnitude == shadow_magnitude,
            "single_hash": _fingerprint(baseline_answer),
            "shadow_hash": _fingerprint(shadow_answer),
        },
        "evidence": {
            "same": _evidence(single) == _evidence(shadow),
            "single": _evidence(single), "shadow": _evidence(shadow),
        },
    }
    return {
        "version": 1, "single_trace_id": single.trace_id,
        "shadow_trace_id": shadow.trace_id,
        "status": "MATCH" if all(item["same"] for item in layers.values())
                  else "DIFFERENT",
        "layers": layers,
    }
