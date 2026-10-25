"""Optional official τ-bench integration tests: run with .tau-venv."""
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langsmith import tracing_context

try:
    version('tau2')
except PackageNotFoundError:
    pytest.skip('Run in .tau-venv to test official τ-bench integration', allow_module_level=True)

from evals.tau_clinic import verify_upstream, run_trial
# Upstream imports dotenv at module load; unit tests must not load local credentials.
with patch('dotenv.load_dotenv', return_value=False):
    verify_upstream()
from tau2.data_model.message import UserMessage
from tau2.user.user_simulator import UserSimulator
from voice_agent.config import Settings
from voice_agent.runtime import AgentRuntime
from evals.multi_agent_cases import scripted_models
from test_multi_agent import NOW


class ResponsiveTestUser(UserSimulator):
    """Protocol double; live evaluations use the unmodified official UserSimulator."""
    def generate_next_message(self, message, state):
        text = message.content or ''
        if 'Please review:' in text:
            answer = 'Confirm booking.'
        elif 'created and verified' in text:
            answer = '###STOP###'
        else:
            answer = 'Book a cleaning for Alex, phone 2025550199, November 3, 2026 at 2 PM.'
        state.messages.append(message)
        result = UserMessage(role='user', content=answer)
        state.messages.append(result)
        return result, state


def test_official_orchestrator_and_evaluator_drive_real_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-test-key')
    dataset = json.loads(Path('evals/tau/clinic-tasks.json').read_text())
    models = scripted_models('mcp-booking')
    def factory(settings, clock):
        return AgentRuntime(settings, clock=clock, model_factory=lambda name: models[name])
    with tracing_context(enabled=False):
        result = run_trial(dataset['cases'][0], 0, tmp_path/'trial', Settings(data_dir=tmp_path), NOW,
                           'anthropic/synthetic', 8, 300, runtime_factory=factory, user_factory=ResponsiveTestUser)
    assert result['error_type'] is None, result
    assert result['reward'] == 1, result
    assert result['termination'] == 'user_stop'
    grade = json.loads((tmp_path/'trial/sql-grade.json').read_text())
    assert grade['passed']
    assert len(grade['outcomes']['bookings']) == 1
    assert result['turns'] == 2
    # Verifier is a read-only observer; there are no extra model calls during grading.
    assert len(models['supervisor'].steps) == 8  # Only the two live turns consumed model decisions.


def test_abnormal_termination_cannot_pass_even_when_sql_goal_is_met(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-test-key')
    dataset = json.loads(Path('evals/tau/clinic-tasks.json').read_text())
    models = scripted_models('mcp-booking')
    def factory(settings, clock):
        return AgentRuntime(settings, clock=clock, model_factory=lambda name: models[name])
    class NeverStops(ResponsiveTestUser):
        def generate_next_message(self, message, state):
            result, state = super().generate_next_message(message, state)
            if result.content == '###STOP###':
                result.content = 'Please repeat the appointment details.'
            return result, state
    with tracing_context(enabled=False):
        result = run_trial(dataset['cases'][0], 0, tmp_path/'trial', Settings(data_dir=tmp_path), NOW,
                           'anthropic/synthetic', 2, 300, runtime_factory=factory, user_factory=NeverStops)
    from evals.tau_checks import grade_sql
    meta = json.loads((tmp_path/'trial/evidence.json').read_text())
    grade = grade_sql(tmp_path/'trial/state/business.sqlite3', meta['call_id'], dataset['cases'][0]['expected'])
    assert grade['passed']  # SQL goal was actually met before the conversation exceeded its budget.
    assert result['reward'] == 0
    assert result['error_type'] == 'RuntimeError'
    assert result.get('termination') not in {'user_stop', 'agent_stop'}
