from pathlib import Path
import json
import tomllib
from evals.harbor_judge_export import export


def test_judge_export_has_runtime_only_credentials_and_frozen_evidence(tmp_path,monkeypatch):
    fixture=tmp_path/'trial'
    (fixture/'state').mkdir(parents=True)
    for name in ('task.json','evidence.json','simulation.json','result.json','state/business.sqlite3'):
        (fixture/name).write_text('fixture')
    monkeypatch.setenv('ANTHROPIC_API_KEY','never-bake-this-test-secret')
    monkeypatch.setattr('evals.harbor_judge_export.source_trials',lambda p:iter([({'case_id':'clinic-create','trial':'0'},fixture,None)]))
    dest=tmp_path/'export'
    manifest=export(dest,Path('unused.json'))
    task=dest/'tasks/clinic-create-0'
    config=tomllib.loads((task/'task.toml').read_text())
    assert config['verifier']['env']['ANTHROPIC_API_KEY']=='${ANTHROPIC_API_KEY:-}'
    assert 'env' not in config['agent']
    assert 'historical' not in manifest['credentials']
    assert not any(b'never-bake-this-test-secret' in p.read_bytes() for p in dest.rglob('*') if p.is_file())
    assert not list(dest.rglob('.env'))
    assert (task/'environment/evidence/trial/state/business.sqlite3').read_text()=='fixture'
    job=json.loads((dest/'job.json').read_text())
    assert job['n_attempts']==1


def test_report_compares_long_task_names_using_task_config(tmp_path,monkeypatch):
    from evals.harbor_judge_report import report
    from evals.judge_core import DIMENSIONS
    name='clinic-review-without-consent-fresh'
    baseline=tmp_path/'baseline.json'
    baseline.write_text(json.dumps({'results':[{'case_id':'clinic-review-without-consent','trial':'fresh','quality_score_100':85,'verdict':'needs_review'}]}))
    (tmp_path/'manifest.json').write_text(json.dumps({'tasks':[name],'source_summary':str(baseline),'scope':'test'}))
    job=tmp_path/'jobs/test'
    trial=job/'clinic-review-without-consent-fr__suffix'
    verifier=trial/'verifier'
    verifier.mkdir(parents=True)
    (trial/'config.json').write_text(json.dumps({'task':{'path':str(tmp_path/'tasks'/name)}}))
    result={'verdict':'pass','quality_score_100':90,'scores_0_to_4':dict.fromkeys(DIMENSIONS,3),'container_marker':True}
    (verifier/'sandbox-result.json').write_text(json.dumps(result))
    monkeypatch.setattr('evals.harbor_judge_report.trial_metrics',lambda path:{'call_id':'one','target_action':None,'new_booking_verified':False,'business_success':True,'human_escalation':False,'expected_escalation':False,'simulation_duration_ms':1000})
    summary=report(tmp_path,job)
    assert summary['same_evidence_comparison'][0]['task']==name
    assert summary['same_evidence_comparison'][0]['prior_verdict']=='needs_review'
