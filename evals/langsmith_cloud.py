"""Explicit upload and cloud read-back checks; never equate flush with verification."""
from datetime import datetime, timezone
import time
import warnings
from uuid import UUID, uuid5

from langsmith import tracing_context


def read_run(client, run_id):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        return client.read_run(run_id)


def verify_run(client, run_id, attempts=6):
    error_type = None
    for attempt in range(attempts):
        try:
            run = read_run(client, run_id)
            if run.end_time is not None:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', DeprecationWarning)
                    url = client.get_run_url(run=run)
                return run, {'id': str(run.id), 'name': run.name, 'trace_id': str(run.trace_id),
                             'project_id': str(run.session_id), 'url': url, 'end_time': run.end_time.isoformat(),
                             'error': bool(run.error), 'verified_at': datetime.now(timezone.utc).isoformat()}
        except Exception as error:
            error_type = type(error).__name__
        if attempt+1 < attempts:
            time.sleep(min(0.5*2**attempt, 4))
    raise RuntimeError('LangSmith cloud run not verified: ' + (error_type or 'incomplete_run'))


def trace_tree(client, root):
    # No limit > 100: current Cloud paginates at a maximum of 100 per page.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        runs = list(client.list_runs(project_id=root.session_id, trace_id=root.trace_id,
                                    select=['id', 'name', 'run_type', 'trace_id', 'session_id', 'parent_run_id',
                                            'parent_run_ids', 'start_time', 'end_time', 'error']))
    return {'spans': len(runs), 'llm_spans': sum(r.run_type == 'llm' for r in runs),
            'tool_spans': sum(r.run_type == 'tool' for r in runs),
            'tool_names': sorted({r.name for r in runs if r.run_type == 'tool'}),
            'run_ids': [str(r.id) for r in runs],
            'incomplete_spans': [str(r.id) for r in runs if r.end_time is None]}


def upload_feedback(client, source, judge_run_id, judgment, result, evidence_sha256):
    """Attach session-scoped model feedback to the real source run with stable IDs."""
    rows = []
    for dimension, score in result['scores_0_to_4'].items():
        rating = getattr(judgment, dimension)
        rows.append(('judge_'+dimension, score/4, rating.rationale+' Evidence: '+', '.join(rating.evidence_refs), 'model', None))
    rows += [('judge_quality', result['quality_score_100']/100, judgment.summary, 'model', None),
             ('judge_verdict', 1 if result['verdict']=='pass' else 0, 'Whole-conversation verdict; needs_review is distinct from a business failure.', 'model', result['verdict']),
             ('sql_pass', int(result['sql_pass']), 'Independent SQL assertion gate, not a model judgment.', 'api', None)]
    expected_ids = {}
    with tracing_context(enabled=False):
        for key, score, comment, kind, value in rows:
            feedback_id = str(uuid5(UUID(judge_run_id), key))
            client.create_feedback(run_id=source.id, trace_id=source.trace_id, session_id=source.session_id,
                                   start_time=source.start_time, key=key, score=score, value=value, comment=comment,
                                   feedback_source_type=kind, source_run_id=judge_run_id if kind=='model' else None,
                                   source_info={'scope': 'whole_conversation', 'evidence_sha256': evidence_sha256,
                                                'judge_run_id': judge_run_id, 'score_scale': '0..1 (original 0..4)'},
                                   feedback_id=feedback_id)
            expected_ids[feedback_id] = {'key': key, 'score': score}
        client.flush(timeout=30)
        found = {}
        for attempt in range(6):
            items = list(client.list_feedback(run_ids=[source.id], limit=100))
            found = {str(f.id): f for f in items if str(f.id) in expected_ids}
            if set(found) == set(expected_ids) and all(abs(float(found[i].score)-row['score']) < 1e-6 for i,row in expected_ids.items()):
                return {'verified': True, 'count': len(found), 'feedback_ids': list(found),
                        'keys': sorted(f.key for f in found.values()), 'source_run_id': str(source.id)}
            time.sleep(min(0.5*2**attempt, 4))
    raise RuntimeError('Judge feedback not fully readable from LangSmith Cloud')
