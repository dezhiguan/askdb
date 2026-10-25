"""Named full prompt templates, shared by local copies and platform execution."""
from __future__ import annotations

import importlib
import re
from functools import lru_cache

DEFINITIONS = {
    "sql": ("askdb.llm", "SYSTEM"),
    "fast": ("askdb.agent", "FAST_SYSTEM"),
    "intent": ("askdb.agent", "INTENT_SYSTEM"),
    "query-agent": ("askdb.agent", "AGENT_SYSTEM"),
    "plan": ("askdb.planner", "PLAN_SYSTEM"),
    "replan": ("askdb.planner", "REPLAN_SYSTEM"),
    "assess": ("askdb.planner", "ASSESS_SYSTEM"),
    "supervisor": ("askdb.multiagent.supervisor_graph", "SUPERVISOR_SYSTEM"),
    "supervisor-decide": ("askdb.multiagent.supervisor_graph", "DECIDE_SYSTEM"),
    "semantic": ("askdb.multiagent.semantic_agent", "SYSTEM"),
    "verifier": ("askdb.multiagent.verifier_agent", "OPINION_SYSTEM"),
    "synthesizer": ("askdb.multiagent.synthesizer_agent", "SYSTEM"),
}


@lru_cache(maxsize=1)
def templates():
    result = []
    for name, (module, constant) in DEFINITIONS.items():
        template = getattr(importlib.import_module(module), constant)
        template = template.replace("{rule9}{rule10}", "{sql_rules}")
        # Some static prompts contain JSON braces. Only interpret fields that
        # are Python identifiers; leave literal JSON intact.
        fields = list(re.finditer(r"\{([a-zA-Z_]\w*)\}", template))
        cursor, pattern, body, names = 0, [], [], []
        for field in fields:
            literal = template[cursor:field.start()]
            pattern.append(re.escape(literal))
            names.append(field.group(1))
            pattern.append("(.*?)")
            body.extend((literal, "{{" + field.group(1) + "}}"))
            cursor = field.end()
        pattern.append(re.escape(template[cursor:]))
        body.append(template[cursor:])
        if name in {"fast", "intent", "query-agent"}:
            names.append("methodology")
            pattern.append(r"(\n\n【方法论 · 必须遵循】.*)?")
            body.append("{{methodology}}")
        result.append((name, re.compile("".join(pattern), re.DOTALL), names, "".join(body)))
    return result


def resolve(rendered: str):
    for name, pattern, fields, _ in templates():
        match = pattern.fullmatch(rendered)
        if match:
            return name, {field: value or "" for field, value in zip(fields, match.groups())}
    raise ValueError("未登记的 AskDB 提示词；请先补齐平台清单和提示词副本")
