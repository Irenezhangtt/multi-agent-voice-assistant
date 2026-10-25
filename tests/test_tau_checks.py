import json

import pytest
from langsmith import tracing_context

from evals.tau_checks import grade_sql
from evals.tau_clinic import summarize
from evals.multi_agent_cases import scripted_models, FIELDS
from test_multi_agent import runtime_with


def test_sql_grader_covers_actual_mcp_create_modify_and_cancel(tmp_path, monkeypatch):
    models = scripted_models('mcp-booking')
    with tracing_context(enabled=False):
        runtime = runtime_with(tmp_path, monkeypatch, models)
        try:
            call_id = runtime.create_call()['state']['call_id']
            messages = ['Book a cleaning for Alex, phone 2025550199, November 3 at 2 PM', 'Confirm booking.',
                        'Move it to November 4 at 3 PM', 'Confirm change.', 'Cancel it', 'Confirm cancellation.']
            for i, message in enumerate(messages):
                runtime.turn(call_id, message, f'tau-{i}')
            expected = {'booking_count': 1, 'handoff_count': 0, 'mutation_actions': ['create', 'modify', 'cancel'],
                        'booking': {**FIELDS, 'customer_phone': '+12025550199', 'date': '2026-11-04', 'time': '15:00', 'status': 'cancel', 'revision': 3}}
            grade = grade_sql(tmp_path/'business.sqlite3', call_id, expected)
            assert grade['passed'], grade
            # Changing only the saved natural-language claim cannot make a wrong SQL target pass.
            wrong = {**expected, 'booking': {**expected['booking'], 'time': '14:00'}}
            assert not grade_sql(tmp_path/'business.sqlite3', call_id, wrong)['passed']
            with pytest.raises(ValueError):
                grade_sql(tmp_path/'business.sqlite3', call_id, {**expected, 'unsupported_metric': True})
        finally:
            runtime.close()


def test_repeated_trial_metrics_measure_all_success_not_any_success():
    rows = [{'case_id': 'a', 'reward': 1}, {'case_id': 'a', 'reward': 0},
            {'case_id': 'b', 'reward': 1}, {'case_id': 'b', 'reward': 1}]
    summary = summarize(rows)
    assert summary['success_rate'] == 0.75
    assert summary['pass_hat_1'] == 0.75
    assert summary['pass_hat_2'] == 0.5
    assert summary['observed_all_trials_success_rate'] == 0.5
