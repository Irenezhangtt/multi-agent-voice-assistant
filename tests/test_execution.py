"""Cancellation races, process recovery and bounded specialist context."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json

from langchain_core.messages import AIMessage
import pytest

from voice_agent.context import specialist_context
from voice_agent.execution import ExecutionInterrupted, write_scope
from voice_agent.runtime import AgentRuntime
from test_multi_agent import ScriptedModel, call, runtime_with, FIELDS


def blocking(started, release, answer):
    def invoke(messages):
        started.set()
        assert release.wait(10), 'test did not release its model'
        return answer
    return invoke


def test_new_message_finishes_before_old_model_returns(tmp_path, monkeypatch):
    started, release = Event(), Event()
    supervisor = ScriptedModel([blocking(started, release, call('delegate_handoff', request='Old request')),
                                AIMessage(content='New request acknowledged')])
    runtime = runtime_with(tmp_path, monkeypatch, {'supervisor': supervisor})
    pool = ThreadPoolExecutor(1)
    try:
        call_id = runtime.create_call()['state']['call_id']
        old = pool.submit(runtime.turn, call_id, 'I want a human', 'old')
        assert started.wait(5)
        latest = runtime.turn(call_id, 'Actually, just tell me what you can do', 'new')
        assert latest['state']['route'] == 'clarify'
        with pytest.raises(ExecutionInterrupted):
            old.result(3)
        assert not release.is_set()  # The stale provider call is still running.
        assert runtime.store.outcomes(call_id)['transfers'] == []
        assert runtime.store.get_turn(call_id, 'old') is None
        assert len(runtime.store.traces(call_id)) == 1
        with runtime.store.connect() as db:
            assert db.execute("SELECT status FROM executions WHERE request_id='old'").fetchone()[0] == 'superseded'
    finally:
        release.set()
        pool.shutdown()
        runtime.close()


def test_resume_restarts_at_unfinished_step_after_new_runtime(tmp_path, monkeypatch):
    started, release = Event(), Event()
    models = {'supervisor': ScriptedModel([call('delegate_booking', request='Prepare requested booking')]),
              'booking': ScriptedModel([call('find_booking', customer_phone=FIELDS['customer_phone']),
                                       blocking(started, release, AIMessage(content='old unfinished output'))])}
    runtime = runtime_with(tmp_path, monkeypatch, models)
    pool = ThreadPoolExecutor(1)
    call_id = runtime.create_call()['state']['call_id']
    old = pool.submit(runtime.turn, call_id, 'Book the appointment', 'recover-me')
    try:
        assert started.wait(5)
        runtime.interrupt(call_id)
        with pytest.raises(ExecutionInterrupted):
            old.result(3)
        assert runtime.get_call(call_id)['execution']['status'] == 'suspended'
    finally:
        release.set()
        pool.shutdown()
        runtime.close()
    supervisor = ScriptedModel([AIMessage(content='Review this offer')])
    booking = ScriptedModel([call('prepare_appointment', action='create', fields=FIELDS)])
    recovered = runtime_with(tmp_path, monkeypatch, {'supervisor': supervisor, 'booking': booking})
    calls = []
    actual = recovered.multi_agent.toolkit.call
    def observed(agent, name, arguments, scope):
        calls.append(name)
        return actual(agent, name, arguments, scope)
    recovered.multi_agent.toolkit.call = observed
    try:
        result = recovered.resume(call_id)
        assert result['state']['status'] == 'awaiting_confirmation'
        assert calls == ['prepare_appointment']  # Cached model + find_booking did not run again.
        assert len(booking.inputs) == len(supervisor.inputs) == 1
        assert recovered.get_call(call_id)['execution'] is None
        assert recovered.store.get_turn(call_id, 'recover-me')
        assert recovered.store.traces(call_id)[0]['resumed'] is True
    finally:
        recovered.close()


def test_commit_receipt_survives_lost_rpc_result_and_process_restart(tmp_path, monkeypatch):
    models = {'supervisor': ScriptedModel([call('delegate_booking', request='Prepare'), AIMessage(content='Review'),
                                         call('delegate_booking', request='Commit')]),
              'booking': ScriptedModel([call('prepare_appointment', action='create', fields=FIELDS), call('commit_appointment')])}
    runtime = runtime_with(tmp_path, monkeypatch, models)
    call_id = runtime.create_call()['state']['call_id']
    runtime.turn(call_id, 'Book requested cleaning', 'prepare')
    actual = runtime.multi_agent.toolkit.call
    def lose_response(agent, name, arguments, scope):
        result = actual(agent, name, arguments, scope)
        if name == 'commit_appointment':
            runtime.interrupt(call_id)
            raise ExecutionInterrupted('Simulated disconnect after SQL commit')
        return result
    runtime.multi_agent.toolkit.call = lose_response
    try:
        with pytest.raises(ExecutionInterrupted):
            runtime.turn(call_id, 'Confirm booking.', 'commit')
        assert len(runtime.store.outcomes(call_id)['bookings']) == 1
    finally:
        runtime.close()
    recovered = runtime_with(tmp_path, monkeypatch, {'supervisor': ScriptedModel([AIMessage(content='Done')]),
                                                   'booking': ScriptedModel([])})
    try:
        result = recovered.resume(call_id)
        assert result['state']['booking']['status'] == 'confirmed'
        assert 'already committed' in result['state']['reply']
        with recovered.store.connect() as db:
            assert db.execute('SELECT count(*) FROM booking_mutations').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM bookings').fetchone()[0] == 1
    finally:
        recovered.close()


def test_superseded_task_cannot_write_even_inside_repository_transaction(tmp_path, monkeypatch):
    runtime = runtime_with(tmp_path, monkeypatch, {'supervisor': ScriptedModel([])})
    try:
        call_id = runtime.create_call()['state']['call_id']
        old = runtime.submit(call_id, 'Old booking', 'old')
        control = runtime.journal.activate(old['id'])
        runtime.submit(call_id, 'New request', 'new')
        token = write_scope.set(control.scope())
        try:
            with pytest.raises(ExecutionInterrupted):
                runtime.backend.mutate_booking(call_id, FIELDS['customer_phone'], 'create', 'old-write',
                                              FIELDS['patient_name'], FIELDS['service'], FIELDS['date'], FIELDS['time'])
        finally:
            write_scope.reset(token)
        assert runtime.store.outcomes(call_id)['bookings'] == []
        with pytest.raises(ExecutionInterrupted):
            runtime.journal.activate(old['id'], resume=True)
    finally:
        runtime.close()


def test_child_context_has_only_selected_facts_and_no_parent_or_other_agent_messages():
    state = {'message': 'Move it to 3pm', 'history': [{'role': 'user', 'content': 'PARENT-HISTORY-SECRET'}] * 40,
             'booking': {'status': 'awaiting_confirmation', 'customer_phone': '+12025550199', 'time': '14:00'},
             'insurance': {'query_id': 'IQ-1', 'status': 'active', 'payer': 'Aetna', 'member_id': 'DEMO123', 'evidence': ['LARGE-RAW-DOCUMENT'] * 100},
             'agent_results': {'insurance': {'reply': 'OTHER-AGENT-TRANSCRIPT'}}}
    child = specialist_context('booking', state, 'Change the requested time')
    encoded = json.dumps(child)
    assert child['pending_task']['time'] == '14:00'
    assert child['identity_verified'] is False
    assert child['trusted_facts'] == {}  # Pending offer is not a committed booking.
    assert all(secret not in encoded for secret in ['PARENT-HISTORY-SECRET', 'OTHER-AGENT-TRANSCRIPT', 'LARGE-RAW-DOCUMENT', 'IQ-1'])
    selected = specialist_context('booking', state, 'Check relevant eligibility', ['insurance'])
    assert selected['trusted_facts']['insurance']['value']['status'] == 'active'
    assert specialist_context('insurance', state, 'Continue the same assessment')['pending_task']['member_id'] == 'DEMO123'
    assert len(json.dumps(selected)) < 1500
    with pytest.raises(ValueError):
        specialist_context('booking', state, 'Read other domains', ['handoff'])


def test_insurance_retrieval_can_resume_after_mcp_server_restart(tmp_path, monkeypatch):
    from langchain_core.messages import ToolMessage
    started, release = Event(), Event()
    models = {'supervisor': ScriptedModel([call('delegate_insurance', request='Assess Aetna DEMO123 for cleaning')]),
              'insurance': ScriptedModel([call('retrieve_insurance_evidence', payer='Aetna', member_id='DEMO123', service='cleaning'),
                                         blocking(started, release, AIMessage(content='unfinished assessment'))])}
    runtime = runtime_with(tmp_path, monkeypatch, models)
    pool = ThreadPoolExecutor(1)
    call_id = runtime.create_call()['state']['call_id']
    old = pool.submit(runtime.turn, call_id, 'Check Aetna DEMO123 for cleaning', 'insurance-resume')
    try:
        assert started.wait(5)
        runtime.interrupt(call_id)
        with pytest.raises(ExecutionInterrupted):
            old.result(3)
    finally:
        release.set()
        pool.shutdown()
        runtime.close()
    def assess(messages):
        evidence = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
        return call('record_insurance_assessment', payer='Aetna', member_id='DEMO123', service='cleaning', kb_sha256=evidence['kb_sha256'],
                    assessment={'status': 'active', 'reason': 'Demo policy and enrollment', 'citations': ['demo-aetna-rule', 'demo-member-active']})
    recovered = runtime_with(tmp_path, monkeypatch, {'supervisor': ScriptedModel([AIMessage(content='Done')]), 'insurance': ScriptedModel([assess])})
    try:
        result = recovered.resume(call_id)
        assert result['state']['insurance']['status'] == 'active'
        assert len(recovered.store.outcomes(call_id)['insurance_queries']) == 1
    finally:
        recovered.close()


def test_provider_timeout_does_not_turn_into_an_infinite_poll(tmp_path, monkeypatch):
    runtime = runtime_with(tmp_path, monkeypatch, {'supervisor': ScriptedModel([TimeoutError('synthetic provider timeout')])})
    try:
        call_id = runtime.create_call()['state']['call_id']
        result = runtime.turn(call_id, 'What can you do?', 'timeout')
        assert result['state']['status'] == 'model_error'
        assert runtime.get_call(call_id)['execution'] is None
    finally:
        runtime.close()


def test_delegation_schema_rejects_field_names_then_allows_valid_retry(tmp_path, monkeypatch):
    supervisor = ScriptedModel([
        call('delegate_booking', request='Prepare the booking', fact_keys=['customer_phone', 'date', 'time']),
        call('delegate_booking', request='Prepare the booking', fact_keys=['booking']),
        AIMessage(content='Review the verified offer'),
    ])
    booking = ScriptedModel([call('prepare_appointment', action='create', fields=FIELDS)])
    runtime = runtime_with(tmp_path, monkeypatch, {'supervisor': supervisor, 'booking': booking})
    try:
        call_id = runtime.create_call()['state']['call_id']
        result = runtime.turn(call_id, 'Book the requested cleaning')
        assert result['state']['status'] == 'awaiting_confirmation'
        assert len(booking.inputs) == 1
        assert len([e for e in result['state']['events'] if e.get('type') == 'delegation']) == 1
    finally:
        runtime.close()
