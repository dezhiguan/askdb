"""Real structured-contract coverage for branches outside the default query path.

Only the authenticated evaluation entry enables this protocol. All generations
use the same domain schemas, platform prompts and gateway as normal execution.
SQL outputs additionally traverse the normal guarded SDK tool on synthetic data.
"""
from __future__ import annotations

import importlib
import json

CONTRACTS = {
    'sql': ('askdb.llm', 'SqlDraft'),
    'fast': ('askdb.agent', 'FastSql'),
    'plan': ('askdb.planner', 'Plan'),
    'replan': ('askdb.planner', 'Plan'),
    'assess': ('askdb.planner', 'Assessment'),
    'supervisor': ('askdb.multiagent.supervisor_graph', 'SupervisorPlanDraft'),
    'supervisor-decide': ('askdb.multiagent.supervisor_graph', 'SupervisorDecision'),
    'semantic': ('askdb.multiagent.semantic_agent', 'SemanticContractDraft'),
    'verifier': ('askdb.multiagent.verifier_agent', 'VerifierOpinion'),
    'synthesizer': ('askdb.multiagent.synthesizer_agent', 'SynthesisDraft'),
}


def parse(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and value.get('keel_eval') == 'structured-contract-v1':
        if value.get('prompt') not in CONTRACTS or not isinstance(value.get('human'), str):
            raise ValueError('Unknown evaluation contract')
        return value
    return None


def run(case, cfg):
    from .keel_prompts import DEFINITIONS
    from .keel_runtime import active
    from .llm import LlmClient, INTERVAL_EXAMPLE
    from .tools import ToolContext
    name = case['prompt']
    module, constant = DEFINITIONS[name]
    system = getattr(importlib.import_module(module), constant)
    if name == 'sql':
        system = system.format(dialect=cfg.dialect, interval_example=INTERVAL_EXAMPLE[cfg.dialect])
    schema_module, schema_name = CONTRACTS[name]
    schema = getattr(importlib.import_module(schema_module), schema_name)
    parsed, _ = LlmClient(cfg).structured(schema, system, case['human'])
    output = {'parsed': parsed.model_dump(mode='json')}
    if name in {'sql', 'fast'}:
        invocation = active.get()
        result = invocation.call(invocation.tool('execute_sql', {'sql': parsed.sql},
                                                ToolContext(cfg, org_id=65)))
        output['execution'] = result.to_dict()
    return json.dumps(output, ensure_ascii=False, default=str)
