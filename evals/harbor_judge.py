"""Harbor sandbox verifier: recheck frozen SQL, then independently call the judge."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import socket


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['replay','verify'])
    args=parser.parse_args()
    from evals.judge_core import evidence_bundle
    directory=Path('/evidence/trial')
    bundle, provenance=evidence_bundle(directory)
    if args.phase=='replay':
        target=Path('/logs/agent/replay.json')
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps({'mode':'historical_real_call_replay','case_id':bundle['task_id'],**provenance}))
        return
    from langsmith import Client
    from evals.business_metrics import trial_metrics
    from evals.llm_judge import judge_trial
    from voice_agent.config import Settings
    output=Path('/logs/verifier')
    output.mkdir(parents=True,exist_ok=True)
    result={'verdict':'error','reward':0,'scope':'historical_real_call_replay',
            'execution_environment':'harbor-docker-verifier','hostname':socket.gethostname(),
            'container_marker':Path('/.dockerenv').exists()}
    try:
        replay=json.loads(Path('/logs/agent/replay.json').read_text())
        if replay['evidence_sha256'] != provenance['evidence_sha256']:
            raise ValueError('Replay evidence changed')
        metrics=trial_metrics(directory)
        working=Path('/tmp/judge-trial')
        shutil.copytree(directory,working)
        (working/'sql-grade.json').write_text(json.dumps({'passed':metrics['business_success']}))
        settings=Settings.from_env()
        settings=replace(settings,model=os.getenv('JUDGE_MODEL') or settings.model)
        os.environ['EVAL_EXECUTION_ENVIRONMENT']='harbor-docker-verifier'
        client=Client(timeout_ms=15000)
        root=json.loads(Path('/evidence/metadata.json').read_text()).get('feedback_run_id')
        judged=judge_trial(working,output/'judge',settings,client,feedback_run_id=root)
        result.update(judged,metrics=metrics,reward=int(judged['verdict']=='pass'))
    except Exception as error:
        result.update(error_type=type(error).__name__)
    (output/'sandbox-result.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
    (output/'reward.txt').write_text(str(result['reward'])+'\n')
    print(json.dumps({k:result[k] for k in ('verdict','reward','execution_environment','container_marker')}))


if __name__=='__main__':
    main()
