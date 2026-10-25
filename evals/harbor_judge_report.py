"""Summarize actual Harbor verifier outputs without confusing reward with SQL success."""
import argparse
from collections import Counter
import json
from pathlib import Path

from evals.business_metrics import aggregate, render, trial_metrics
from evals.harbor_judge_export import source_trials
from evals.llm_judge import summarize


def report(export, job=None):
    export=Path(export)
    manifest=json.loads((export/'manifest.json').read_text())
    jobs=sorted((export/'jobs').glob('*/result.json'))
    job=Path(job) if job else jobs[-1].parent
    results=[]
    for path in sorted(job.glob('*/verifier/sandbox-result.json')):
        r=json.loads(path.read_text())
        r['harbor_trial']=path.parent.parent.name
        config=json.loads((path.parent.parent/'config.json').read_text())
        # Harbor truncates long trial display names; use the canonical task path.
        r['task_name']=Path(config['task']['path']).name
        r['artifact']=str(path.resolve())
        results.append(r)
    metrics=[]
    for task in manifest['tasks']:
        directory=export/'tasks'/task/'environment/evidence/trial'
        metrics.append(trial_metrics(directory))
    business=aggregate(metrics)
    baseline=json.loads(Path(manifest['source_summary']).read_text())
    lookup={r['case_id']+'-'+str(r['trial']):r for r in baseline['results']}
    comparison=[]
    for r in results:
        name=r['task_name']
        previous=lookup.get(name)
        if previous and 'quality_score_100' in r:
            comparison.append({'task':name,'prior_score':previous.get('quality_score_100'),
                               'sandbox_score':r['quality_score_100'],'prior_verdict':previous['verdict'],
                               'sandbox_verdict':r['verdict']})
    summary={**summarize(results),'planned_trials':len(manifest['tasks']),
             'missing_verifier_results':len(manifest['tasks'])-len(results),
             'container_verified':sum(r.get('container_marker') is True for r in results),
             'business_metrics':business,'scope':manifest['scope'],'job':str(job.resolve()),
             'same_evidence_comparison':comparison,'results':results}
    (export/'summary.json').write_text(json.dumps(summary,indent=2,default=str)+'\n')
    quality=f"{summary['mean_quality_score_100']:.1f}" if summary['mean_quality_score_100'] is not None else 'N/A'
    counts=Counter(r['verdict'] for r in results)
    lines=[render(business),'## Harbor Sandbox Judge', '',
           f"已得到 {len(results)}/{len(manifest['tasks'])} 个 verifier 结果；Docker 标记验证 {summary['container_verified']} 个；评分错误 {summary['errors']} 个。", '',
           f"Judge：{counts['pass']} 通通过，{counts['needs_review']} 通需要复查，{counts['fail']} 通失败；平均质量分 {quality}/100；Cloud feedback 回读确认 {summary['cloud_feedback_verified']} 通。", '',
           'Harbor reward=1 表示 Judge pass 且 SQL 通过；needs_review 的 reward=0 是质量未达到阈值，不等于 SQL 业务失败。基础设施或模型错误单独保留，不作为有效低分。', '',
           '这里在 Harbor Docker 内重新评判已冻结的真实文本对话和 SQL，并非在容器中重新发起 19 通业务对话。业务 Agent 与 Judge 的提示词及上下文独立；本次使用同一 Claude 模型，仍存在共同盲点。', '',
           '| Harbor trial | Verdict | 质量分 | Judge Cloud trace |','|---|---|---:|---|']
    for r in results:
        url=r.get('cloud',{}).get('judge',{}).get('url')
        link=f'[打开]({url})' if url else '未验证'
        lines.append(f"| {r['harbor_trial']} | {r['verdict']} | {r.get('quality_score_100','N/A')} | {link} |")
    changed=[r for r in comparison if r['prior_score']!=r['sandbox_score']]
    flipped=[r for r in comparison if r['prior_verdict']!=r['sandbox_verdict']]
    lines += ['', '## 同证据评分波动', '',
              f'与此前宿主机 cohort 对比，{len(comparison)} 个可比较样本中，{len(changed)} 个总分变化、{len(flipped)} 个 verdict 变化。业务 Agent 和对话证据没有因此改变，不能把这类差异解释为 Agent 改进。前一轮还包含有效工具格式输出，本轮全部采用原生 JSON schema，输出方式也可能影响评分。', '',
              '固定输入、代码和依赖可复现实验过程，但云模型评分不保证确定性。同一模型的独立提示词和 Sandbox 不能代替人工校准；SQL 仍是硬门槛。', '',
              '| Task | 此前分数 / Verdict | Sandbox 分数 / Verdict |','|---|---|---|']
    for r in changed:
        lines.append(f"| {r['task']} | {r['prior_score']} / {r['prior_verdict']} | {r['sandbox_score']} / {r['sandbox_verdict']} |")
    lines += ['',f"Harbor job：`{job.resolve()}`。镜像与代码／输入 hash 见本目录 `manifest.json`。",'',
              '凭据仅通过 verifier 的运行时环境变量模板注入，未复制到镜像。Docker 具有进程／文件系统隔离，允许联网调用 Claude 与 LangSmith；并非断网环境。', '',
              '平均通话时长应由通话结束时间减开始时间计算，暂停／重连须合并同一 call_id。挂断率应为任务完成前客户主动结束的通话数除以具有完整结束事件的通话数，并按阶段细分；网络断线、模拟 USER_STOP、正常完成结束应分别记录。当前缺少这些真实事件，保持 N/A。', '']
    (export/'report.md').write_text('\n'.join(lines))
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--export',type=Path,required=True)
    p.add_argument('--job',type=Path)
    args=p.parse_args()
    result=report(args.export,args.job)
    print(json.dumps({k:v for k,v in result.items() if k not in {'results','business_metrics','same_evidence_comparison'}}))


if __name__=='__main__':
    main()
