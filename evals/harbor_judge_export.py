"""Freeze real-call artifacts into standard Harbor judge-replay tasks, without keys."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

from evals.harbor_export import ROOT, encoded, DEFAULT_BASE_IMAGE


def source_trials(summary_path):
    summary_path=Path(summary_path).resolve()
    summary=json.loads(summary_path.read_text())
    manifest=json.loads((summary_path.parent/'manifest.json').read_text())
    prior=Path(manifest.get('source_cohort',summary_path.parent))
    origin=json.loads((prior/'manifest.json').read_text()) if prior != summary_path.parent else manifest
    for r in summary['results']:
        if r.get('scope')=='fresh':
            directory=prior/'live'/r['case_id']/'trial'
            root=r.get('live_cloud_root',{}).get('id')
        else:
            directory=Path(origin['source'])/r['case_id']/str(r['trial'])
            root=None
        yield r,directory,root


def export(output, summary, limit=None):
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    build=output/'image'
    src=build/'source'
    source_files=[*sorted((ROOT/'voice_agent').rglob('*.py')), *sorted((ROOT/'voice_agent/prompts').glob('*.md')),
                  *sorted((ROOT/'voice_agent/knowledge').glob('*.json')), *sorted((ROOT/'evals').glob('*.py')),
                  ROOT/'evals/prompts/clinic-judge.md', ROOT/'requirements.lock']
    hashes={}
    for p in source_files:
        target=src/p.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(p.read_bytes())
        hashes[str(p.relative_to(ROOT))]=sha256(p.read_bytes()).hexdigest()
    image='clinic-judge:'+sha256(encoded(hashes)).hexdigest()[:16]
    (build/'Dockerfile').write_text(f'FROM {DEFAULT_BASE_IMAGE}\nWORKDIR /opt/voice-agent\nCOPY source/ ./\nRUN pip install --no-cache-dir -r requirements.lock\nENV PYTHONPATH=/opt/voice-agent LANGSMITH_TRACING=false\n')
    env_keys=['ANTHROPIC_API_KEY','ANTHROPIC_WORKSPACE_ID','ANTHROPIC_MODEL','LANGSMITH_API_KEY','LANGSMITH_ENDPOINT',
              'LANGSMITH_PROJECT','LANGSMITH_AGENT_ID','LANGSMITH_AGENT_ENVIRONMENT','LANGSMITH_WORKSPACE_ID',
              'JUDGE_PROVIDER','JUDGE_MODEL','JUDGE_API_KEY','OPENAI_API_KEY','JUDGE_BASE_URL']
    env={key:'${'+key+':-}' for key in env_keys if os.getenv(key)}
    env['LANGSMITH_TRACING']='true'
    trials=list(source_trials(summary))
    if limit is not None:
        trials=trials[:limit]
    tasks=[]
    for r,directory,root in trials:
        name=r['case_id']+'-'+str(r['trial'])
        task=output/'tasks'/name
        environment=task/'environment'
        evidence=environment/'evidence'
        evidence.mkdir(parents=True)
        for filename in ('task.json','evidence.json','simulation.json','result.json','state/business.sqlite3'):
            target=evidence/'trial'/filename
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(directory/filename,target)
        (evidence/'metadata.json').write_bytes(encoded({'feedback_run_id':root,'source':str(directory.resolve())}))
        (environment/'Dockerfile').write_text(f'FROM {image}\nCOPY evidence/ /evidence/\nRUN mkdir -p /logs/agent /logs/verifier\n')
        (task/'instruction.md').write_text('Replay frozen real-call evidence without changing the business state. Judge executes only during verification.\n')
        (task/'task.toml').write_text('schema_version = "1.3"\n[metadata]\ncategory = "clinic-llm-judge-replay"\n[agent]\ntimeout_sec = 30\n[verifier]\ntimeout_sec = 240\n[verifier.env]\n'+''.join(f'{key} = {json.dumps(value)}\n' for key,value in env.items())+'[environment]\nnetwork_mode = "public"\ncpus = 1\nmemory_mb = 1024\nstorage_mb = 2048\n')
        tests=task/'tests'
        tests.mkdir()
        (tests/'test.sh').write_text('#!/bin/bash\nset -euo pipefail\npython -m evals.harbor_judge verify\n')
        tasks.append(name)
    (output/'harbor_judge_adapter.py').write_bytes((ROOT/'evals/harbor_judge_agent.py').read_bytes())
    job={'n_attempts':1,'n_concurrent_trials':2,'jobs_dir':str(output/'jobs'),
         'environment':{'type':'docker','delete':True},'agents':[{'import_path':'harbor_judge_adapter:EvidenceReplayAgent'}],
         'datasets':[{'path':str(output/'tasks')}]}
    (output/'job.json').write_bytes(encoded(job))
    manifest={'scope':'Harbor Docker re-evaluation of frozen real call traces; business agent not rerun',
              'source_summary':str(Path(summary).resolve()),'image':image,'source_hashes':hashes,'tasks':tasks,
              'judge_provider':os.getenv('JUDGE_PROVIDER','anthropic'),'judge_model':os.getenv('JUDGE_MODEL') or os.getenv('ANTHROPIC_MODEL'),
              'network':'public egress for model and LangSmith APIs; filesystem/process sandbox; no host app data mounted',
              'credentials':'Runtime verifier.env templates only; never copied into image or task files'}
    manifest['export_hashes']={str(p.relative_to(output)):sha256(p.read_bytes()).hexdigest() for p in output.rglob('*') if p.is_file()}
    (output/'manifest.json').write_bytes(encoded(manifest))
    return manifest


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ROOT/'reports/judge/clinic-final-cloud/summary.json')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--limit',type=int)
    args=p.parse_args()
    manifest=export(args.output,args.source,args.limit)
    print(json.dumps({k:manifest[k] for k in ('scope','image','tasks','judge_provider','judge_model')}))


if __name__=='__main__':
    main()
