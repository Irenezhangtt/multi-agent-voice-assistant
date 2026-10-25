from types import SimpleNamespace
from uuid import uuid4

from evals.judge_core import Judgment, verdict
from evals.langsmith_cloud import upload_feedback
from test_judge import good_judgment


def test_feedback_is_read_back_and_preserves_model_vs_sql_source():
    class Client:
        def __init__(self):
            self.rows=[]
        def create_feedback(self, **kwargs):
            self.rows.append(kwargs)
        def flush(self, **kwargs):
            pass
        def list_feedback(self, **kwargs):
            return [SimpleNamespace(id=r['feedback_id'],score=r['score'],key=r['key']) for r in self.rows]
    c=Client()
    source=SimpleNamespace(id=uuid4(),trace_id=uuid4(),session_id=uuid4(),start_time=None)
    judgment=Judgment.model_validate(good_judgment())
    result=verdict(judgment,True)
    proof=upload_feedback(c,source,str(uuid4()),judgment,result,'digest')
    assert proof['verified'] and proof['count']==8
    assert all(r['session_id']==source.session_id and r['trace_id']==source.trace_id for r in c.rows)
    assert all(r['source_info']['scope']=='whole_conversation' for r in c.rows)
    assert next(r for r in c.rows if r['key']=='sql_pass')['feedback_source_type']=='api'
    assert next(r for r in c.rows if r['key']=='judge_factual_grounding')['feedback_source_type']=='model'
