from copy import deepcopy

import pytest
from pydantic import ValidationError

from evals.judge_core import DIMENSIONS, Judgment, validate_judgment, verdict, evidence_bundle
from test_multi_agent import call, runtime_with, ScriptedModel, FIELDS
from langchain_core.messages import AIMessage
from langsmith import tracing_context


def good_judgment():
    return {**{d:{'score':4,'rationale':'Supported by the evidence','evidence_refs':['turn_1']} for d in DIMENSIONS},
            'findings':[], 'summary':'The task was completed correctly.', 'improvements':[]}


def test_judge_cannot_override_sql_gate_or_cite_nonexistent_evidence():
    bundle={'evidence_refs':['turn_1']}
    raw=good_judgment()
    judgment=validate_judgment(raw,bundle)
    assert verdict(judgment,True)['verdict']=='pass'
    assert verdict(judgment,False)['verdict']=='fail'
    raw['policy_adherence']['evidence_refs']=['fabricated_tool_999']
    with pytest.raises(ValueError):
        validate_judgment(raw,bundle)


def test_critical_findings_and_quality_issues_have_distinct_verdicts():
    raw=good_judgment()
    raw['findings']=[{'severity':'critical','category':'unauthorized_write','description':'A write occurred without confirmation.', 'evidence_refs':['turn_1']}]
    assert verdict(Judgment.model_validate(raw),True)['verdict']=='fail'
    raw['findings']=[]
    raw['conversation_quality']['score']=2
    result=verdict(Judgment.model_validate(raw),True)
    assert result['verdict']=='needs_review'
    assert result['quality_score_100']==90
    raw['task_completion']['score']=2
    assert verdict(Judgment.model_validate(raw),True)['verdict']=='fail'
    raw['task_completion']['score']=5
    with pytest.raises(ValidationError):
        Judgment.model_validate(raw)


def test_evidence_bundle_reads_sql_and_hides_prior_rewards(tmp_path,monkeypatch):
    import json
    models={'supervisor':ScriptedModel([call('delegate_booking',request='Prepare the booking'),AIMessage(content='Review')]),
            'booking':ScriptedModel([call('prepare_appointment',action='create',fields=FIELDS)])}
    with tracing_context(enabled=False):
        runtime=runtime_with(tmp_path/'state',monkeypatch,models)
        try:
            call_id=runtime.create_call()['state']['call_id']
            response=runtime.turn(call_id,'Please review a cleaning for Alex, phone 2025550199, November 3 at 2 PM')
        finally:
            runtime.close()
    (tmp_path/'evidence.json').write_text(json.dumps({'call_id':call_id,'seed_id':None}))
    (tmp_path/'task.json').write_text(json.dumps({'id':'review','user_scenario':{'instructions':'Only prepare an offer.'},'evaluation_criteria':{'hidden':'SECRET_REFERENCE'}}))
    (tmp_path/'simulation.json').write_text(json.dumps({'messages':[{'role':'user','content':'Please review a cleaning.'},{'role':'assistant','content':response['state']['reply']}]}))
    (tmp_path/'sql-grade.json').write_text(json.dumps({'passed':True,'hidden':'SECRET_SQL_REWARD'}))
    bundle,meta=evidence_bundle(tmp_path)
    assert meta['trace_ids']==[response['trace_id']]
    assert bundle['sql_evidence']['booking']==[]
    assert 'sql_state' in bundle['evidence_refs']
    assert any(t['node']=='prepare_appointment' for t in bundle['tool_trajectory'])
    encoded=json.dumps(bundle)
    assert 'SECRET_REFERENCE' not in encoded and 'SECRET_SQL_REWARD' not in encoded


def test_judge_model_preserves_temperature_through_structured_output(monkeypatch):
    from voice_agent.config import Settings
    from evals.llm_judge import judge_model
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-test-key')
    model=judge_model(Settings(mode='claude'))
    structured=model.with_structured_output(Judgment)
    bound=structured.steps[0]
    assert bound.bound.temperature==0
    assert bound.bound.default_request_timeout==120
    assert bound.bound.max_retries==0


def test_evaluator_uses_provider_native_schema_and_validates_references():
    from evals.judge_core import evaluate
    class Model:
        def with_structured_output(self,schema,method=None):
            assert schema is Judgment
            assert method=='json_schema'
            return self
        def invoke(self,messages,config=None):
            return good_judgment()
    assert evaluate({'evidence_refs':['turn_1']},Model()).task_completion.score==4
