"""Fail-closed checks for cross-source evidence composition."""

from __future__ import annotations

from itertools import combinations

from .protocol import JoinContract


class FederationPolicyError(ValueError):
    pass


def parse_contracts(raw: list[dict]) -> list[JoinContract]:
    return [JoinContract.model_validate(item) for item in raw]


def approved_contracts(source_ids: set[str], contracts: list[JoinContract]) -> list[JoinContract]:
    """Require an explicit aggregate-only contract for every source pair in a run."""
    if len(source_ids) <= 1:
        return []
    approved: list[JoinContract] = []
    for left, right in combinations(sorted(source_ids), 2):
        match = next((item for item in contracts if
                      {item.left_source, item.right_source} == {left, right}
                      and item.aggregate_only), None)
        if match is None:
            raise FederationPolicyError(
                f"数据源 {left} 与 {right} 没有已登记的聚合 JoinContract")
        approved.append(match)
    return approved
