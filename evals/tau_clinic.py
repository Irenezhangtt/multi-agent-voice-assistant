"""Run the real clinic agent with official τ-bench text simulation and grading."""
import argparse
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

# Avoid fetching LiteLLM's mutable remote pricing map during a pinned evaluation.
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'true')

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'evals/tau/clinic-tasks.json'
UPSTREAM = ROOT / 'evals/tau-upstream.json'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def verify_upstream(path=None):
    pin = json.loads(UPSTREAM.read_text())
    path = Path(path or ROOT / '.benchmarks/tau2-bench').resolve()
    actual = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(path), 'diff', '--quiet', 'HEAD'], check=False).returncode
    if actual != pin['commit'] or dirty:
        raise ValueError('τ-bench checkout does not match the clean pinned commit')
    if version('tau2') != pin['version']:
        raise ValueError('Installed tau2 version does not match the pin')
    # Some bundled Python runtimes do not expand editable .pth paths.
    # Select only the already verified checkout, never an arbitrary source path.
    sys.path.insert(0, str(path / 'src'))
    import tau2
    if Path(tau2.__file__).resolve() != path / 'src/tau2/__init__.py':
        raise ValueError('Installed tau2 must point to the pinned local checkout')
    return pin


def snapshot_manifest(dataset):
    files = [*sorted((ROOT/'voice_agent').rglob('*.py')), *sorted((ROOT/'voice_agent/prompts').glob('*.md')),
             *sorted((ROOT/'voice_agent/knowledge').glob('*.json')),
             *(ROOT/'evals'/name for name in ('tau_clinic.py', 'tau_adapter.py', 'tau_checks.py', 'tau-upstream.json')),
             ROOT/'requirements-tau.lock']
    hashes = {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in files}
    hashes['dataset'] = sha256(dataset.read_bytes()).hexdigest()
    return {'sha256': sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), 'files': hashes}


def make_task(case):
    from tau2.data_model.tasks import Task
    return Task.model_validate({
        'id': case['id'],
        'user_scenario': {'instructions': case['instructions']},
        'agent_opener': 'Hello. I can manage local appointments, check fictional demo insurance evidence, or record human escalation. How can I help?',
        'evaluation_criteria': {'reward_basis': ['ENV_ASSERTION'], 'env_assertions': [{
            'env_type': 'assistant', 'func_name': 'assert_sql_outcome', 'arguments': {'expected': case['expected']}}]},
    })


def summarize(trials):
    groups = defaultdict(list)
    for trial in trials:
        groups[trial['case_id']].append(trial)
    successful = sum(t['reward'] == 1 for t in trials)
    all_pass = sum(all(t['reward'] == 1 for t in items) for items in groups.values())
    def reliability(k):
        eligible = [items for items in groups.values() if len(items) >= k]
        estimates = [math.comb(sum(t['reward'] == 1 for t in items), k)/math.comb(len(items), k) for items in eligible]
        return sum(estimates)/len(estimates) if estimates else None
    return {'trials': len(trials), 'successful_trials': successful, 'failed_trials': len(trials)-successful,
            'success_rate': successful/len(trials) if trials else None,
            'cases': len(groups), 'cases_passing_every_observed_trial': all_pass,
            'observed_all_trials_success_rate': all_pass/len(groups) if groups else None,
            'pass_hat_1': reliability(1), 'pass_hat_2': reliability(2),
            'per_case': {name: {'passed': sum(t['reward'] == 1 for t in items), 'trials': len(items)} for name, items in groups.items()}}


def run_trial(case, trial, output, settings, now, user_model, max_turns, seed, runtime_factory=None, user_factory=None):
    from tau2.data_model.simulation import RewardInfo, TerminationReason
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.user.user_simulator import UserSimulator
    from voice_agent.runtime import AgentRuntime
    from evals.tau_adapter import ClinicRuntimeAgent, clinic_environment

    output.mkdir(parents=True, exist_ok=False)
    state_dir = output/'state'
    trial_settings = replace(settings, mode='claude', architecture='multi-agent', data_dir=state_dir)
    runtime = None
    start = perf_counter()
    result = {'case_id': case['id'], 'trial': trial, 'seed': seed, 'reward': 0.0, 'error_type': None}
    try:
        runtime = (runtime_factory or AgentRuntime)(trial_settings, clock=lambda: now)
        call_id = runtime.create_call()['state']['call_id']
        seed_id = None
        if case.get('seed_booking'):
            row = runtime.backend.mutate_booking('tau-seed', action='create', idempotency_key='tau-seed', **case['seed_booking'])
            if row['status'] != 'confirmed':
                raise RuntimeError('Could not seed trial appointment')
            seed_id = row['id']
        write_json(output/'evidence.json', {'call_id': call_id, 'seed_id': seed_id})
        task = make_task(case)
        write_json(output/'task.json', task.model_dump(mode='json'))
        headers = {}
        if user_model.startswith('anthropic/') and os.getenv('ANTHROPIC_WORKSPACE_ID', '').strip():
            headers['anthropic-workspace-id'] = os.environ['ANTHROPIC_WORKSPACE_ID'].strip()
        user = (user_factory or UserSimulator)(llm=user_model, instructions=str(task.user_scenario), tools=None,
                                              llm_args={'temperature': 0, 'max_tokens': 600, 'timeout': 30, 'num_retries': 0, 'extra_headers': headers})
        agent = ClinicRuntimeAgent(runtime, call_id, max_turns=max_turns)
        simulation = Orchestrator(domain='clinic_local', agent=agent, user=user, environment=clinic_environment(output),
                                  task=task, max_steps=max_turns*2+2, max_errors=2, seed=seed,
                                  validate_communication=True, timeout=240).run()
        # Save official conversation data without provider raw response blobs.
        serialized = simulation.model_dump(mode='json')
        for message in serialized.get('messages', []):
            message.pop('raw_data', None)
        write_json(output/'simulation.json', serialized)
        grade = EnvironmentEvaluator.calculate_reward(lambda **kwargs: clinic_environment(output, **kwargs), task, simulation.messages)
        if simulation.termination_reason not in {TerminationReason.USER_STOP, TerminationReason.AGENT_STOP}:
            grade = RewardInfo(reward=0.0, info={'termination': simulation.termination_reason.value, 'sql_reward_before_termination_gate': grade.reward})
        write_json(output/'reward.json', grade.model_dump(mode='json'))
        write_json(output/'agent-turns.json', agent.responses)
        result.update(reward=grade.reward, call_id=call_id, termination=simulation.termination_reason.value,
                      turns=len(agent.responses), turn_latency_ms=agent.durations,
                      user_cost_usd_estimate=simulation.user_cost, agent_cost_usd_estimate=None)
        with sqlite3.connect(state_dir/'business.sqlite3') as db:
            payloads = [json.loads(r[0]) for r in db.execute("SELECT payload FROM execution_steps WHERE step_key LIKE '%:model:%'")]
        usages = [p[0]['data'].get('usage_metadata') or {} for p in payloads]
        result['agent_usage'] = {'model_calls': len(payloads), 'input_tokens': sum(u.get('input_tokens', 0) for u in usages),
                                 'output_tokens': sum(u.get('output_tokens', 0) for u in usages)}
    except Exception as error:
        # Keep failure trials in the denominator; do not print credentials/provider exception bodies.
        result['error_type'] = type(error).__name__
    finally:
        if runtime:
            try:
                runtime.close()
            except Exception as error:
                result['cleanup_error_type'] = type(error).__name__
        result['duration_ms'] = round((perf_counter()-start)*1000, 2)
        write_json(output/'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--trials', type=int, default=2)
    parser.add_argument('--max-turns', type=int, default=8)
    parser.add_argument('--seed', type=int, default=300)
    parser.add_argument('--user-model', default=None)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.trials <= 5 or not 1 <= args.max_turns <= 12:
        parser.error('trials must be 1..5 and max-turns 1..12')
    dataset = json.loads(args.dataset.read_text())
    if dataset.get('official_task_set') is not False:
        parser.error('Clinic runner accepts explicitly custom clinic datasets only')
    cases = dataset['cases']
    ids = [c['id'] for c in cases]
    if len(set(ids)) != len(ids) or any(not re.fullmatch('[a-z0-9-]+', i) for i in ids):
        parser.error('Case IDs must be unique safe directory names')
    if args.cases:
        if set(args.cases)-set(ids):
            parser.error('Unknown case selection')
        cases = [c for c in cases if c['id'] in args.cases]
    if not cases:
        parser.error('No cases selected')
    pin = verify_upstream()
    from loguru import logger
    logger.remove()
    if args.dry_run:
        print(json.dumps({'scope': pin['scope'], 'cases': [c['id'] for c in cases], 'trials': len(cases)*args.trials, 'upstream_commit': pin['commit']}))
        return
    from voice_agent.config import Settings
    settings = Settings.from_env()
    # A fresh directory is mandatory; preserve all previous successful and failed runs.
    output = (args.output or ROOT/'reports/tau'/f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid4().hex[:6]}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    user_model = args.user_model or 'anthropic/'+settings.model
    manifest = {'scope': pin['scope'], 'official_leaderboard_score': False, 'domain': dataset['domain'],
                'dataset_version': dataset['version'], 'upstream': pin, 'snapshot': snapshot_manifest(args.dataset),
                'agent_versions': replace(settings, mode='claude', architecture='multi-agent').versions(),
                'user_model': user_model, 'trial_count_per_case': args.trials, 'max_turns': args.max_turns,
                'grading': 'Official ENV_ASSERTION reward + independent read-only SQLite checks; normal-termination gate',
                'modalities': ['text'], 'agent_cost_note': 'Agent model tokens recorded; no assumed zero-dollar cost',
                'repeatability_note': 'Pinned inputs/versions/seeds; cloud model responses are not guaranteed deterministic'}
    write_json(output/'manifest.json', manifest)
    write_json(output/'dataset.json', dataset)
    trials = []
    for case in cases:
        for trial in range(args.trials):
            print(json.dumps({'stage': 'starting', 'case': case['id'], 'trial': trial}), flush=True)
            result = run_trial(case, trial, output/case['id']/str(trial), settings, datetime.fromisoformat(dataset['now']),
                               user_model, args.max_turns, args.seed+trial)
            trials.append(result)
            write_json(output/'summary.json', {**summarize(trials), 'results': trials, 'scope': pin['scope']})
            print(json.dumps({'stage': 'finished', 'case': case['id'], 'trial': trial, 'reward': result['reward'],
                              'turns': result.get('turns'), 'error_type': result['error_type']}), flush=True)
    print(json.dumps({'output': str(output), **summarize(trials)}), flush=True)


if __name__ == '__main__':
    main()
