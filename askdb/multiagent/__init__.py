"""Multi-agent contracts and runtime components.

The package is deliberately dependency-light at the protocol boundary so the
same payloads can be persisted in checkpoints, returned by the API and rendered
by the web client without reconstructing agent-internal objects.
"""

from .protocol import (
    AgentRole,
    Budget,
    Claim,
    Evidence,
    MultiAgentArtifacts,
    QuerySpec,
    RepairTask,
    Review,
    ReviewVerdict,
    SkillBinding,
    SubTask,
    SubTaskStatus,
    TaskPlan,
    single_agent_artifacts,
)

__all__ = [
    "AgentRole",
    "Budget",
    "Claim",
    "Evidence",
    "MultiAgentArtifacts",
    "QuerySpec",
    "RepairTask",
    "Review",
    "ReviewVerdict",
    "SkillBinding",
    "SubTask",
    "SubTaskStatus",
    "TaskPlan",
    "single_agent_artifacts",
]
