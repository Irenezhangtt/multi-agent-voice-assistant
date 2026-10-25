import json
from types import SimpleNamespace
import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict
from evals.judge_providers import HTTPJudge


class Example(BaseModel):
    model_config=ConfigDict(extra='forbid')
    score:int


def test_openai_judge_uses_native_schema_and_handles_refusal(monkeypatch):
    monkeypatch.setenv('JUDGE_API_KEY','test-secret')
    calls=[]
    data={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"score":3}'}]}]}
    def post(url,**kwargs):
        calls.append((url,kwargs))
        return SimpleNamespace(is_error=False,json=lambda:data)
    monkeypatch.setattr('evals.judge_providers.httpx.post',post)
    judge=HTTPJudge('openai','explicit-test-model').with_structured_output(Example,method='json_schema')
    assert judge.invoke([HumanMessage('Evaluate evidence')]).score==3
    assert calls[0][0]=='https://api.openai.com/v1/responses'
    body=calls[0][1]['json']
    assert body['store'] is False and body['text']['format']['strict'] is True
    assert 'temperature' not in body
    data['output'][0]['content']=[{'type':'refusal','refusal':'no'}]
    with pytest.raises(RuntimeError,match='refused'):
        judge.invoke([HumanMessage('Evaluate')])


def test_provider_http_error_never_exposes_body(monkeypatch):
    monkeypatch.setenv('JUDGE_API_KEY','test-secret')
    monkeypatch.setattr('evals.judge_providers.httpx.post',lambda *a,**k:SimpleNamespace(is_error=True,status_code=401))
    judge=HTTPJudge('openai','test').with_structured_output(Example,method='json_schema')
    with pytest.raises(RuntimeError,match='HTTP 401'):
        judge.invoke([HumanMessage('Evaluate')])
