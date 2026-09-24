"""Skill package manifest and static safety validation."""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..protocol import AgentRole


_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_FORBIDDEN_INSTRUCTION = re.compile(
    r"(?i)(api[_ -]?key|password|passwd|secret|token)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(os\.system|subprocess\.|eval\s*\(|exec\s*\()")


class SkillStatus(str, Enum):
    DRAFT = "draft"
    SHADOW = "shadow"
    PUBLISHED = "published"
    DISABLED = "disabled"
    REVOKED = "revoked"


class SkillKind(str, Enum):
    GENERAL = "general"
    DOMAIN = "domain"
    SOURCE = "source"
    ANALYSIS = "analysis"
    VERIFICATION = "verification"
    PRESENTATION = "presentation"


class SkillTriggers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intents: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: str
    version: str
    status: SkillStatus = SkillStatus.DRAFT
    owner: str
    kind: SkillKind = SkillKind.DOMAIN
    description: str = ""
    agent_roles: list[AgentRole]
    source_scopes: list[str] = Field(default_factory=lambda: ["*"])
    triggers: SkillTriggers = Field(default_factory=SkillTriggers)
    priority: int = Field(default=100, ge=0, le=10_000)
    requires: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)
    requested_tools: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    checksum: str = ""

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("skill id must be a lowercase dotted identifier")
        return value

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not _VERSION_RE.fullmatch(value):
            raise ValueError("skill version must use semantic x.y.z form")
        return value

    @field_validator("instructions")
    @classmethod
    def safe_instructions(cls, values: list[str]) -> list[str]:
        for value in values:
            if _FORBIDDEN_INSTRUCTION.search(value):
                raise ValueError("skill instructions contain a secret or executable code")
        return values

    @model_validator(mode="after")
    def normalize(self) -> "SkillManifest":
        if not self.agent_roles:
            raise ValueError("skill must allow at least one agent role")
        if self.id in {item.split("@", 1)[0] for item in self.requires}:
            raise ValueError("skill cannot require itself")
        if self.id in set(self.conflicts_with):
            raise ValueError("skill cannot conflict with itself")
        if not self.checksum:
            payload = self.model_dump(mode="json", exclude={"checksum"})
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str).encode()
            self.checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


def version_tuple(value: str) -> tuple[int, int, int]:
    if not _VERSION_RE.fullmatch(value):
        raise ValueError(f"invalid semantic version: {value}")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def split_requirement(value: str) -> tuple[str, str]:
    skill_id, marker, constraint = value.partition("@")
    return skill_id, constraint if marker else ""


def version_matches(version: str, constraint: str) -> bool:
    if not constraint or constraint == "*":
        return True
    actual = version_tuple(version)
    if constraint.startswith("^"):
        required = version_tuple(_expand_version(constraint[1:]))
        return actual[0] == required[0] and actual >= required
    return actual == version_tuple(_expand_version(constraint))


def _expand_version(value: str) -> str:
    parts = value.split(".")
    if not all(part.isdigit() for part in parts) or len(parts) > 3:
        raise ValueError(f"unsupported version constraint: {value}")
    return ".".join(parts + ["0"] * (3 - len(parts)))
