"""In-process immutable-view registry for versioned Skill packages."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .manifest import SkillManifest, SkillStatus, split_requirement, version_matches, version_tuple
from ..protocol import AgentRole


class SkillRegistry:
    def __init__(self, manifests: Iterable[SkillManifest] = ()) -> None:
        self._items: dict[str, dict[str, SkillManifest]] = defaultdict(dict)
        for manifest in manifests:
            self.register(manifest)

    def register(self, manifest: SkillManifest, *, replace: bool = False) -> None:
        versions = self._items[manifest.id]
        if manifest.version in versions and not replace:
            raise ValueError(f"skill version already exists: {manifest.ref}")
        versions[manifest.version] = manifest.model_copy(deep=True)

    def list(self, *, include_inactive: bool = True) -> list[SkillManifest]:
        rows = [item.model_copy(deep=True) for versions in self._items.values()
                for item in versions.values()]
        if not include_inactive:
            rows = [item for item in rows if item.status in (
                SkillStatus.PUBLISHED.value, SkillStatus.SHADOW.value)]
        return sorted(rows, key=lambda item: (item.id, version_tuple(item.version)), reverse=True)

    def get(self, skill_id: str, version: str) -> SkillManifest | None:
        item = self._items.get(skill_id, {}).get(version)
        return item.model_copy(deep=True) if item else None

    def resolve_requirement(self, requirement: str, *,
                            allowed_statuses: set[str]) -> SkillManifest | None:
        skill_id, constraint = split_requirement(requirement)
        candidates = [item for item in self._items.get(skill_id, {}).values()
                      if item.status in allowed_statuses
                      and version_matches(item.version, constraint)]
        if not candidates:
            return None
        return max(candidates, key=lambda item: version_tuple(item.version)).model_copy(deep=True)


def build_registry(cfg: Any) -> SkillRegistry:
    """Build a registry from built-ins, legacy YAML rules and configured packages."""
    from ...skill import GENERAL_RULES

    manifests: list[SkillManifest] = [SkillManifest(
        id="builtin.general_data_methodology",
        version="1.0.0",
        status=SkillStatus.PUBLISHED,
        owner="askdb",
        kind="general",
        agent_roles=[AgentRole.SEMANTIC, AgentRole.QUERY_WORKER, AgentRole.VERIFIER],
        instructions=GENERAL_RULES,
        priority=10,
    )]
    raw: dict[str, Any] = getattr(cfg, "raw", {}) or {}
    legacy = list((raw.get("skill") or {}).get("rules") or [])
    if legacy:
        manifests.append(SkillManifest(
            id="builtin.legacy_rules",
            version="1.0.0",
            status=SkillStatus.PUBLISHED,
            owner="deployment",
            kind="source",
            agent_roles=[AgentRole.SEMANTIC, AgentRole.QUERY_WORKER, AgentRole.VERIFIER],
            source_scopes=[getattr(cfg, "source_id", "") or "*"],
            instructions=[str(item) for item in legacy],
            priority=30,
        ))
    for item in list((raw.get("skills") or {}).get("manifests") or []):
        manifests.append(SkillManifest.model_validate(item))
    return SkillRegistry(manifests)
