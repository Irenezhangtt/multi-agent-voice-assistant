"""Official τ-bench interfaces wrapping the unchanged production AgentRuntime.

This is a custom clinic domain. Its reward uses the upstream-supported
ENV_ASSERTION basis against frozen real SQLite evidence, not airline/retail DB hashes.
"""
from pathlib import Path
import json
from time import perf_counter

from tau2.agent.base_agent import HalfDuplexAgent
from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.toolkit import ToolKitBase

from evals.tau_checks import grade_sql

POLICY = '''This is the project's local clinic multi-agent domain. The evaluated agent
uses its own production system prompts, ReAct loops, MCP toolkit and SQL store.
Appointment writes require a review and explicit confirmation in a later turn.
Insurance evidence is fictional. Human escalation records a marker, not a phone transfer.
Task goals, expected outcomes and SQL grader assertions are not passed to the agent.'''


class ClinicEvidenceTools(ToolKitBase):
    """Read-only evaluator surface; no callable tool exposes goals to the agent."""
    def __init__(self, trial_dir):
        super().__init__(DB())
        self.trial_dir = Path(trial_dir)

    def assert_sql_outcome(self, expected: dict) -> bool:
        """Independently check persisted business evidence after the conversation."""
        meta = json.loads((self.trial_dir / 'evidence.json').read_text())
        result = grade_sql(self.trial_dir / 'state/business.sqlite3', meta['call_id'], expected, meta.get('seed_id'))
        (self.trial_dir / 'sql-grade.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result['passed']


def clinic_environment(trial_dir, solo_mode=False):
    if solo_mode:
        raise ValueError('Clinic domain requires a conversational user')
    return Environment(domain_name='clinic_local', policy=POLICY, tools=ClinicEvidenceTools(trial_dir))


class ClinicRuntimeAgent(HalfDuplexAgent):
    """Adapt messages only; all business reasoning still runs in the real agent."""
    def __init__(self, runtime, call_id, max_turns=8):
        super().__init__(tools=[], domain_policy=POLICY)
        if runtime.multi_agent is None:
            raise ValueError('τ evaluation requires the real multi-agent architecture')
        self.runtime, self.call_id, self.max_turns = runtime, call_id, max_turns
        self.responses, self.durations = [], []

    def get_init_state(self, message_history=None):
        if message_history and any(isinstance(m, UserMessage) for m in message_history):
            raise ValueError('Historical user turns must not be silently ignored')
        return 0

    def generate_next_message(self, message, state):
        if not isinstance(message, UserMessage) or message.is_tool_call() or not message.content:
            raise ValueError('Clinic adapter accepts text user messages only')
        if state >= self.max_turns:
            raise RuntimeError('Clinic turn budget exhausted')
        start = perf_counter()
        response = self.runtime.turn(self.call_id, message.content, f'tau-{state}')
        self.durations.append((perf_counter()-start)*1000)
        self.responses.append(response)
        return AssistantMessage(role='assistant', content=response['state']['reply']), state+1
