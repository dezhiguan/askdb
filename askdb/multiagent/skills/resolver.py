"""Deterministic Skill discovery, dependency resolution and safe binding."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from .manifest import SkillKind, SkillManifest, SkillStatus, version_tuple
from .registry import SkillRegistry
from ..protocol import AgentRole, SkillBinding


class ResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResolutionContext:
    agent_role: AgentRole | str
    source_id: str
    question: str = ""
    intent: str = ""
    metrics: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    runtime_allowed_tools: frozenset[str] = frozenset()
    agent_allowed_tools: frozenset[str] = frozenset()
    include_shadow: bool = False
    max_skills: int = 8


@dataclass
class ResolutionReport:
    bindings: list[SkillBinding] = field(default_factory=list)
    manifests: list[SkillManifest] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    effective_tools: set[str] = field(default_factory=set)

    def instructions(self) -> list[str]:
        return [line for manifest in self.manifests for line in manifest.instructions]


_KIND_RANK = {
    SkillKind.GENERAL.value: 10,
    SkillKind.DOMAIN.value: 20,
    SkillKind.SOURCE.value: 30,
    SkillKind.ANALYSIS.value: 40,
    SkillKind.VERIFICATION.value: 50,
    SkillKind.PRESENTATION.value: 5,
}


def resolve_skills(registry: SkillRegistry, ctx: ResolutionContext) -> ResolutionReport:
    statuses = {SkillStatus.PUBLISHED.value}
    if ctx.include_shadow:
        statuses.add(SkillStatus.SHADOW.value)
    role = ctx.agent_role.value if isinstance(ctx.agent_role, AgentRole) else str(ctx.agent_role)
    report = ResolutionReport()
    candidates: list[tuple[SkillManifest, str]] = []
    for manifest in registry.list():
        if manifest.status not in statuses:
            report.rejected.append({"skill": manifest.ref, "reason": "status_not_active"})
            continue
        if role not in manifest.agent_roles:
            report.rejected.append({"skill": manifest.ref, "reason": "role_not_allowed"})
            continue
        if not any(fnmatch.fnmatchcase(ctx.source_id, scope) for scope in manifest.source_scopes):
            report.rejected.append({"skill": manifest.ref, "reason": "source_out_of_scope"})
            continue
        matched, reason = _triggered(manifest, ctx)
        if not matched:
            report.rejected.append({"skill": manifest.ref, "reason": "trigger_not_matched"})
            continue
        candidates.append((manifest, reason))

    # Candidate discovery can see multiple published versions. A run pins the
    # newest matching version once; older versions remain available for resumed
    # runs and explicit dependency constraints, but are not loaded alongside it.
    newest: dict[str, tuple[SkillManifest, str]] = {}
    for manifest, reason in candidates:
        current = newest.get(manifest.id)
        if current is None or version_tuple(manifest.version) > version_tuple(current[0].version):
            newest[manifest.id] = (manifest, reason)
    candidates = list(newest.values())

    selected: dict[str, tuple[SkillManifest, str]] = {}
    visiting: set[str] = set()

    def add(manifest: SkillManifest, reason: str) -> None:
        if manifest.id in visiting:
            raise ResolutionError(f"skill dependency cycle at {manifest.ref}")
        current = selected.get(manifest.id)
        if current:
            if current[0].version != manifest.version:
                raise ResolutionError(
                    f"incompatible skill versions: {current[0].ref} and {manifest.ref}")
            return
        visiting.add(manifest.id)
        for requirement in manifest.requires:
            dependency = registry.resolve_requirement(requirement, allowed_statuses=statuses)
            if dependency is None:
                raise ResolutionError(
                    f"missing published dependency {requirement} for {manifest.ref}")
            if role not in dependency.agent_roles:
                raise ResolutionError(
                    f"dependency {dependency.ref} does not allow role {role}")
            if not any(fnmatch.fnmatchcase(ctx.source_id, scope)
                       for scope in dependency.source_scopes):
                raise ResolutionError(
                    f"dependency {dependency.ref} is outside source {ctx.source_id}")
            add(dependency, f"dependency_of:{manifest.ref}")
        visiting.remove(manifest.id)
        selected[manifest.id] = (manifest, reason)

    for manifest, reason in sorted(candidates, key=_candidate_key):
        add(manifest, reason)

    ids = set(selected)
    for manifest, _ in selected.values():
        conflicts = ids.intersection(manifest.conflicts_with)
        if conflicts:
            raise ResolutionError(
                f"skill conflict: {manifest.ref} conflicts with {sorted(conflicts)}")

    ordered = sorted(selected.values(), key=_candidate_key, reverse=True)
    if len(ordered) > ctx.max_skills:
        ordered = ordered[:ctx.max_skills]
    runtime_tools = set(ctx.runtime_allowed_tools)
    agent_tools = set(ctx.agent_allowed_tools) if ctx.agent_allowed_tools else runtime_tools
    requested = {tool for manifest, _ in ordered for tool in manifest.requested_tools}
    report.effective_tools = runtime_tools & agent_tools & requested
    for manifest, reason in ordered:
        report.manifests.append(manifest)
        report.bindings.append(SkillBinding(
            skill_id=manifest.id,
            version=manifest.version,
            checksum=manifest.checksum,
            selection_reason=reason,
            effective_tools=sorted(
                set(manifest.requested_tools) & report.effective_tools),
            source="registry",
        ))
    return report


def _candidate_key(item: tuple[SkillManifest, str]) -> tuple[int, int, str]:
    manifest = item[0]
    return (_KIND_RANK.get(str(manifest.kind), 0), manifest.priority, manifest.id)


def _triggered(manifest: SkillManifest, ctx: ResolutionContext) -> tuple[bool, str]:
    trigger = manifest.triggers
    if not any((trigger.intents, trigger.metrics, trigger.tables, trigger.terms)):
        return True, "default"
    if ctx.intent and ctx.intent in trigger.intents:
        return True, f"intent:{ctx.intent}"
    metric = next((item for item in ctx.metrics if item in trigger.metrics), "")
    if metric:
        return True, f"metric:{metric}"
    table = next((item for item in ctx.tables if item in trigger.tables), "")
    if table:
        return True, f"table:{table}"
    term = next((item for item in trigger.terms if item.lower() in ctx.question.lower()), "")
    if term:
        return True, f"term:{term}"
    return False, ""
