"""Business denominators from SQL; never invent phone telemetry from text trials."""
import argparse
import json
from pathlib import Path
import sqlite3


def trial_metrics(directory):
    directory = Path(directory)
    task = json.loads((directory/'task.json').read_text())
    result = json.loads((directory/'result.json').read_text())
    meta = json.loads((directory/'evidence.json').read_text())
    expected = task['evaluation_criteria']['env_assertions'][0]['arguments']['expected']
    with sqlite3.connect((directory/'state/business.sqlite3').resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        mutations = [dict(r) for r in db.execute("SELECT * FROM booking_mutations WHERE request_key != 'tau-seed'")]
        escalation = db.execute("SELECT count(*) FROM transfers WHERE call_id=? AND status='human_escalation'", (meta['call_id'],)).fetchone()[0] > 0
    from evals.tau_checks import grade_sql
    grade = grade_sql(directory/'state/business.sqlite3', meta['call_id'], expected, meta.get('seed_id'))
    actions = [a for a in expected.get('mutation_actions', []) if a != 'create']
    target_action = ('create' if expected.get('booking', {}).get('status') == 'confirmed' else actions[-1] if actions else None)
    success = bool(grade['passed'] and result.get('reward') == 1)
    return {'case_id': task['id'], 'call_id': meta['call_id'], 'target_action': target_action,
            'business_success': success, 'new_booking_verified': target_action == 'create' and success and any(m['action']=='create' for m in mutations),
            'human_escalation': escalation, 'expected_escalation': expected.get('handoff_count', 0)>0,
            'simulation_duration_ms': result.get('duration_ms'), 'error_type': result.get('error_type')}


def aggregate(rows):
    if len({r['call_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate call IDs would bias business metrics')
    def ratio(n, d):
        return {'numerator': n, 'denominator': d, 'rate': n/d if d else None}
    created = [r for r in rows if r['target_action']=='create']
    required = [r for r in rows if r['expected_escalation']]
    not_required = [r for r in rows if not r['expected_escalation']]
    times = [r['simulation_duration_ms'] for r in rows if r['simulation_duration_ms'] is not None]
    return {'calls': len(rows), 'scope': 'custom clinic text simulation; task mix is not production traffic',
            'booking_success': ratio(sum(r['new_booking_verified'] for r in created), len(created)),
            'business_success': ratio(sum(r['business_success'] for r in rows), len(rows)),
            'human_escalation_marker_rate': ratio(sum(r['human_escalation'] for r in rows), len(rows)),
            'required_escalation_success': ratio(sum(r['human_escalation'] for r in required), len(required)),
            'unnecessary_escalation': ratio(sum(r['human_escalation'] for r in not_required), len(not_required)),
            'mean_simulation_duration_seconds': sum(times)/len(times)/1000 if times else None,
            'simulation_duration_samples': len(times),
            'mean_phone_call_duration_seconds': None, 'user_hangup_rate': None,
            'missing_telemetry_reason': 'No real call start/end, caller-hangup reason or workflow-stage events in these text simulations; USER_STOP is not a hangup.'}


def render(metrics):
    def rate(key):
        r=metrics[key]
        return f"{r['rate']:.1%} ({r['numerator']}/{r['denominator']})" if r['rate'] is not None else 'N/A'
    return '\n'.join(['# 业务指标报告', '', '来源：自定义诊所文本模拟；由实际 SQL 重新核验。样本任务配比不能代表生产流量。', '',
        '| 指标 | 数值 |', '|---|---:|',
        '| 新预约成功率（SQL 验证） | '+rate('booking_success')+' |',
        '| 人工升级标记率（尚非实际电话转接） | '+rate('human_escalation_marker_rate')+' |',
        '| 应升级场景的成功标记率 | '+rate('required_escalation_success')+' |',
        '| 非升级场景的额外升级率 | '+rate('unnecessary_escalation')+' |',
        '| 平均真实通话时长 | N/A：缺少真实通话起止事件 |',
        '| 用户挂断率 | N/A：缺少挂断原因和阶段事件 |', '',
        f"共 {metrics['calls']} 通；所有业务目标成功率：{rate('business_success')}。", '',
        f"文本模拟平均运行耗时：{metrics['mean_simulation_duration_seconds']:.2f} 秒。它包含模型和工具等待，不是电话通话时长。" if metrics['mean_simulation_duration_seconds'] is not None else '文本模拟耗时：N/A。', '',
        'USER_STOP 仅表示模拟用户正常结束，不能用来计算客户提前挂断。取消和改期不混入新预约成功率分母；仅审阅且不授权写入的场景也不计为预约失败。', ''])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=Path('reports/judge/clinic-final-cloud/summary.json'))
    p.add_argument('--output', type=Path, required=True)
    args=p.parse_args()
    from evals.harbor_judge_export import source_trials
    rows=[trial_metrics(directory) for _,directory,_ in source_trials(args.source)]
    metrics=aggregate(rows)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output/'metrics.json').write_text(json.dumps({**metrics,'rows':rows},indent=2)+'\n')
    (args.output/'report.md').write_text(render(metrics))
    print(json.dumps(metrics))


if __name__=='__main__':
    main()
