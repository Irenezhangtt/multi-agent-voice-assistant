import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import perf_counter
from types import SimpleNamespace

from langchain_core.messages import AIMessage
import pytest

from test_multi_agent import ScriptedModel, call, runtime_with, FIELDS
from voice_agent.execution import ExecutionInterrupted
from voice_agent.voice import VoiceSession


def batch(*messages):
    return AIMessage(content='',tool_calls=[tool for message in messages for tool in message.tool_calls])


def test_independent_children_really_overlap_and_merge_their_own_domains(tmp_path,monkeypatch):
    booking_started,insurance_started=Event(),Event()
    def book(messages):
        booking_started.set()
        assert insurance_started.wait(3), 'Insurance never ran concurrently'
        return call('prepare_appointment',action='create',fields=FIELDS)
    def insurance(messages):
        insurance_started.set()
        assert booking_started.wait(3), 'Booking never ran concurrently'
        return call('retrieve_insurance_evidence',payer='Aetna',member_id='MISSING999',service='cleaning')
    def assess(messages):
        import json
        from langchain_core.messages import ToolMessage
        evidence=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))
        return call('record_insurance_assessment',payer='Aetna',member_id='MISSING999',service='cleaning',kb_sha256=evidence['kb_sha256'],
                    assessment={'status':'unknown','reason':'Enrollment evidence is missing.','citations':['demo-aetna-rule']})
    supervisor=ScriptedModel([batch(call('delegate_booking',request='Prepare'),call('delegate_insurance',request='Check unknown member'))])
    runtime=runtime_with(tmp_path,monkeypatch,{'supervisor':supervisor,'booking':ScriptedModel([book]),'insurance':ScriptedModel([insurance,assess])})
    try:
        call_id=runtime.create_call()['state']['call_id']
        response=runtime.turn(call_id,'Book a cleaning and independently check Aetna MISSING999')
        assert response['state']['booking']['status']=='awaiting_confirmation'
        assert response['state']['insurance']['status']=='unknown'
        assert response['state']['status']=='awaiting_confirmation'
        assert len(supervisor.inputs)==1  # No unnecessary post-batch synthesis call.
        assert any(e['type']=='parallel_dispatch' for e in response['state']['events'])
        with runtime.store.connect() as db:
            keys=[r[0] for r in db.execute('SELECT step_key FROM execution_steps')]
        assert 'booking:model:0' in keys and 'insurance:model:0' in keys
    finally:
        runtime.close()


def test_handoff_in_a_batch_prevents_all_business_delegations(tmp_path,monkeypatch):
    supervisor=ScriptedModel([batch(call('delegate_booking',request='Book'),call('delegate_handoff',request='Human requested'))])
    booking=ScriptedModel([])
    runtime=runtime_with(tmp_path,monkeypatch,{'supervisor':supervisor,'booking':booking,'handoff':ScriptedModel([call('mark_human_escalation',reason='Explicit request')])})
    try:
        response=runtime.turn(runtime.create_call()['state']['call_id'],'Book it but I want a human')
        assert response['state']['status']=='human_escalation'
        assert not booking.inputs
    finally:
        runtime.close()


def test_cross_domain_dependency_is_ordered_before_dependent_agent(tmp_path,monkeypatch):
    import json
    from langchain_core.messages import ToolMessage
    def assess(messages):
        evidence=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))
        return call('record_insurance_assessment',payer='Aetna',member_id='MISSING999',service='cleaning',kb_sha256=evidence['kb_sha256'],
                    assessment={'status':'unknown','reason':'Missing enrollment evidence.','citations':['demo-aetna-rule']})
    supervisor=ScriptedModel([batch(call('delegate_booking',request='Review after checking evidence',fact_keys=['insurance']),
                                    call('delegate_insurance',request='Retrieve evidence first')),AIMessage(content='Review')])
    booking=ScriptedModel([call('prepare_appointment',action='create',fields=FIELDS)])
    runtime=runtime_with(tmp_path,monkeypatch,{'supervisor':supervisor,'booking':booking,
        'insurance':ScriptedModel([call('retrieve_insurance_evidence',payer='Aetna',member_id='MISSING999',service='cleaning'),assess])})
    try:
        response=runtime.turn(runtime.create_call()['state']['call_id'],'Check insurance first, then review an appointment')
        assert json.loads(booking.inputs[0][1].content)['trusted_facts']['insurance']['value']['status']=='unknown'
        assert not any(e['type']=='parallel_dispatch' for e in response['state']['events'])
    finally:
        runtime.close()


def test_early_offer_is_durable_and_can_be_confirmed_while_sibling_is_interrupted(tmp_path,monkeypatch):
    blocked,release,published=Event(),Event(),Event()
    def slow(messages):
        blocked.set()
        release.wait(5)
        return call('retrieve_insurance_evidence',payer='Aetna',member_id='DEMO123',service='cleaning')
    supervisor=ScriptedModel([batch(call('delegate_booking',request='Prepare'),call('delegate_insurance',request='Check')),
                              call('delegate_booking',request='Commit confirmed offer'),AIMessage(content='Done')])
    runtime=runtime_with(tmp_path,monkeypatch,{'supervisor':supervisor,'booking':ScriptedModel([call('prepare_appointment',action='create',fields=FIELDS),call('commit_appointment')]),'insurance':ScriptedModel([slow])})
    pool=ThreadPoolExecutor(1)
    try:
        call_id=runtime.create_call()['state']['call_id']
        row=runtime.submit(call_id,'Prepare appointment and check insurance','old-parallel')
        old=pool.submit(runtime.execute,row,on_reply=lambda text:published.set())
        assert blocked.wait(3) and published.wait(3)
        snapshot=runtime.get_call(call_id)['state']
        assert snapshot['booking']['status']=='awaiting_confirmation'
        assert snapshot['booking']['review_summary'] in snapshot['history'][-1]['content']
        response=runtime.turn(call_id,'Confirm booking.','new-confirm')
        assert response['state']['booking']['status']=='confirmed'
        with pytest.raises(ExecutionInterrupted):
            old.result(3)
        assert len(runtime.store.outcomes(call_id)['bookings'])==1
        assert not runtime.store.outcomes(call_id)['insurance_queries']
        from evals.tau_checks import grade_sql
        grade=grade_sql(tmp_path/'business.sqlite3',call_id,{
            'booking_count':1,'mutation_actions':['create'],'handoff_count':0,
            'booking':{**FIELDS,'customer_phone':'+12025550199','status':'confirmed','revision':1}})
        assert grade['passed'], grade
    finally:
        release.set()
        pool.shutdown()
        runtime.close()


def test_voice_outputs_audio_before_business_turn_finishes():
    async def run():
        release=Event()
        got_audio=asyncio.Event()
        completed=Event()
        messages=[]
        class WS:
            async def send_json(self,message):
                messages.append(message)
                if message['type']=='audio':got_audio.set()
        class Speech:
            def __init__(self,settings):pass
            async def synthesize_stream(self,texts):
                async for text in texts:
                    yield b'\x00\x00'
        class Runtime:
            settings=SimpleNamespace()
            store=SimpleNamespace(voice_event=lambda *args:None)
            def execute(self,row,**kwargs):
                kwargs['on_reply']('A verified result is ready.')
                release.wait(3)
                completed.set()
                return {'state':{'reply':'A verified result is ready.'},'trace_id':'trace'}
        session=VoiceSession(WS(),Runtime(),'call',speech_factory=Speech)
        session.queue.put_nowait(({'request_id':'request','trace_id':'trace'},0,False,perf_counter()))
        task=asyncio.create_task(session.process_turns())
        try:
            await asyncio.wait_for(got_audio.wait(),1)
            assert not completed.is_set()
            assert not any(m['type']=='turn_result' for m in messages)
            release.set()
            for _ in range(100):
                if any(m['type']=='audio_end' for m in messages):break
                await asyncio.sleep(.01)
            assert sum(m['type']=='reply_chunk' for m in messages)==1
            assert sum(m['type']=='audio' for m in messages)==1
            assert any(m['type']=='turn_result' for m in messages)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())
