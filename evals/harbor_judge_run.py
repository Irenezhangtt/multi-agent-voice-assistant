"""Build a frozen judge image, then let Harbor execute each Docker verifier."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from dotenv import load_dotenv
from evals.harbor_export import ROOT
from evals.harbor_judge_export import export


def main():
    load_dotenv(ROOT/'.env')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['prepare','run'])
    p.add_argument('--source',type=Path,default=ROOT/'reports/judge/clinic-final-cloud/summary.json')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--limit',type=int)
    args=p.parse_args()
    if args.output.exists():
        manifest=json.loads((args.output/'manifest.json').read_text())
        from hashlib import sha256
        for relative,digest in manifest['export_hashes'].items():
            if sha256((args.output/relative).read_bytes()).hexdigest()!=digest:
                raise ValueError('Frozen Harbor export was modified')
    else:
        manifest=export(args.output,args.source,args.limit)
    if args.mode=='prepare':
        print(json.dumps({'prepared':str(args.output),'tasks':len(manifest['tasks']),'scope':manifest['scope']}))
        return
    if manifest['judge_provider'] != os.getenv('JUDGE_PROVIDER','anthropic') or manifest['judge_model'] != (os.getenv('JUDGE_MODEL') or os.getenv('ANTHROPIC_MODEL')):
        raise ValueError('Judge config changed; export a new cohort')
    subprocess.run(['docker','info'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['docker','build','-t',manifest['image'],str(args.output/'image')],check=True)
    env=dict(os.environ,HARBOR_TELEMETRY_ENABLED='false',PYTHONDONTWRITEBYTECODE='1')
    env['PYTHONPATH']=str(args.output.resolve())
    subprocess.run([str(ROOT/'.harbor-venv/bin/harbor'),'run','--yes','-c',str((args.output/'job.json').resolve())],env=env,check=True)


if __name__=='__main__':
    main()
