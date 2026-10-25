"""Finish invalid judge attempts without selecting or replacing valid scores.

Same rubric/model/temperature as v2; use provider-native JSON schema for retries.
Original errors, valid lower ratings and cloud trace provenance are retained.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path

from langsmith import Client, trace, tracing_context
from evals.judge_core import JUDGE_PROMPT, JUDGE_VERSION, evidence_bundle
from evals.llm_judge import judge_model, judge_trial, calibration, summarize
from evals.langsmith_cloud import verify_run
from evals.tau_clinic import ROOT, write_json
from voice_agent.config import Settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    old=json.loads((args.source/'manifest.json').read_text())
    settings=Settings.from_env()
    settings=replace(settings,model=os.getenv('JUDGE_MODEL') or settings.model)
    if old.get('judge_provider','anthropic') != os.getenv('JUDGE_PROVIDER','anthropic'):
        raise ValueError('Repair must preserve the original judge provider')
    if old['judge_model']!=settings.model or old['judge_prompt_sha256']!=sha256(JUDGE_PROMPT.read_bytes()).hexdigest():
        raise ValueError('Repair must preserve the original model and rubric')
    args.output.mkdir(parents=True,exist_ok=False)
    write_json(args.output/'manifest.json',{'judge_version':JUDGE_VERSION,'judge_model':settings.model,'temperature':0,
        'timeout_seconds':120,'response_format':'native_json_schema for invalid-attempt retries',
        'rubric_sha256':old['judge_prompt_sha256'],'source_cohort':str(args.source.resolve()),
        'selection_rule':'Adopt every validated score with cloud proof, including low scores; retry only errors or missing judgments.',
        'same_model_family_bias':True,'human_calibrated':False,
        'code_sha256':{n:sha256((ROOT/'evals'/n).read_bytes()).hexdigest() for n in ['judge_core.py','llm_judge.py','langsmith_cloud.py','judge_repair.py']}})
    client=Client(timeout_ms=15000)
    jobs=[]
    for d in sorted(Path(old['source']).glob('*/[0-9]*/result.json')):
        trial=d.parent
        jobs.append({'case_id':trial.parent.name,'trial':trial.name,'scope':'retrospective',
                     'original':args.source/'retrospective'/trial.parent.name/trial.name,'trial_dir':trial,'root':None})
    for directory in sorted((args.source/'live').iterdir()):
        if not directory.is_dir():
            continue
        root_proof=json.loads((directory/'cloud-live-trace.json').read_text())
        jobs.append({'case_id':directory.name,'trial':'fresh','scope':'fresh','original':directory/'judge',
                     'trial_dir':directory/'trial','root':root_proof['root']})
    results=[]
    def process(job):
        previous=job['original']
        target=args.output/job['scope']/job['case_id']/job['trial']
        try:
            if (previous/'judgment.json').exists() and (previous/'cloud-verification.json').exists():
                result=json.loads((previous/'judgment.json').read_text())
                result['cloud']=json.loads((previous/'cloud-verification.json').read_text())
                result['adopted_from']=str(previous.resolve())
                result['rejudged']=False
                target.mkdir(parents=True,exist_ok=False)
            else:
                result=judge_trial(job['trial_dir'],target,settings,client,feedback_run_id=job['root']['id'] if job['root'] else None)
                result['rejudged']=True
                result['previous_error_artifact']=str((previous/'error.json').resolve()) if (previous/'error.json').exists() else None
            result.update(case_id=job['case_id'],trial=job['trial'],scope=job['scope'])
            if job['root']:
                result['live_cloud_root']=job['root']
            write_json(target/'final-result.json',result)
            print(json.dumps({'case':job['case_id'],'trial':job['trial'],'verdict':result['verdict'],
                              'score':result['quality_score_100'],'rejudged':result['rejudged']}),flush=True)
            return result
        except Exception as error:
            result={'case_id':job['case_id'],'trial':job['trial'],'scope':job['scope'],'verdict':'error','error_type':type(error).__name__}
            target.mkdir(parents=True,exist_ok=True)
            write_json(target/'final-error.json',result)
            print(json.dumps(result),flush=True)
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(process,job) for job in jobs]
        for f in as_completed(futures):
            results.append(f.result())
            write_json(args.output/'summary.json',{**summarize(results),'results':results})
    bundle,_=evidence_bundle(jobs[0]['trial_dir'])
    with tracing_context(enabled=True,client=client,metadata={'eval_suite':'judge_calibration','synthetic':True,'judge_version':JUDGE_VERSION}):
        with trace('judge_negative_controls',client=client,inputs={'synthetic':True,'count':2}) as run:
            controls=calibration(judge_model(settings),bundle)
            run.end(outputs={'controls':controls})
    client.flush(timeout=30)
    _,proof=verify_run(client,run.id)
    write_json(args.output/'calibration.json',{'controls':controls,'cloud':proof})
    summary={**summarize(results),'negative_controls_detected':sum(c['detected'] for c in controls),
             'negative_controls_total':len(controls),'results':results}
    write_json(args.output/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='results'}),flush=True)


if __name__=='__main__':
    main()
