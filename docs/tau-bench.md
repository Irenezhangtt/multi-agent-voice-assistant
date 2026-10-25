# τ-bench：诊所多 Agent 的交互评测

这里实际使用 Sierra 官方 `tau2` 的文本 `Orchestrator`、`UserSimulator` 和 `EnvironmentEvaluator`。被测对象为原有 `AgentRuntime`，保留 Supervisor、三个独立 ReAct 子 Agent、真实 stdio MCP、确认规则和 SQL 事务；没有用一个通用客服模型替代项目 Agent。

**当前数据是本项目编写的 clinic_local 测试域，非官方航空/零售任务。结果不属于官方排行榜成绩。** 原生任务依赖各自的数据库、政策和业务工具，不能直接丢给只有诊所工具的 Agent。此集用于验证已有诊所业务。若需要原版 τ-bench 成绩，需要另外实现相应原生领域工具和政策适配，并独立报告。

上游固定为 `tau2 2.0.0`、commit `0c69df456a96aa76a1162591574dad9c427a34d7`，见 `evals/tau-upstream.json`。不修改上游代码，也不安装到运行语音服务的 `.venv` 中。该固定版本要求 Python >=3.12、<3.14。

## 安装和运行

```bash
# 首次安装：下载固定上游源码，建立独立 .tau-venv
bash scripts/tau-eval.sh setup

# 只检查任务与版本，不调用模型
bash scripts/tau-eval.sh run --dry-run

# 最小真实交互测试
bash scripts/tau-eval.sh run --cases clinic-create --trials 1

# 全部八类任务，每类重复两次
bash scripts/tau-eval.sh run --trials 2

# 只测特定场景；输出目录必须不存在，避免覆盖旧结果
bash scripts/tau-eval.sh run \
  --cases clinic-modify clinic-cancel --trials 2 \
  --output reports/tau/my-run
```

使用本机 `.env` 中的 Claude Key，仍需要 `ANTHROPIC_WORKSPACE_ID` 时自动附带。默认客户模拟器也使用 `anthropic/<ANTHROPIC_MODEL>`，可以用 `--user-model` 单独配置；更换提供商需要其相应凭据。正式试验调用真实模型，会产生模型 API 费用。不给模拟客户开放工具，不把 Key 写入数据集、报告或上游仓库。

`.benchmarks/` 和 `.tau-venv/` 均排除于 Git；`requirements-tau.lock` 固定额外环境依赖。安装后可用以下命令检查官方接入与只读评分器，不调用云端模型：

```bash
LANGSMITH_TRACING=false .tau-venv/bin/python -m pytest -q tests/test_tau_checks.py tests/test_tau_integration.py
```

## 八类场景

| case_id | 用户目标 | SQL / 状态判据 |
|---|---|---|
| clinic-create | 创建并确认预约 | 一条 confirmed 记录、一次 create 回执、字段正确 |
| clinic-modify | 对已有预约改期 | 同一预约 ID、modified、revision 递增、一次 modify |
| clinic-cancel | 取消已有预约 | 保留原行、cancel、revision 递增、一次 cancel |
| clinic-insurance-active | 查询 DEMO123 | SQL 评估为 active，含预期政策与参保引用、演示证据标识 |
| clinic-insurance-unknown | 查询缺失参保资料 | SQL 评估为 unknown，不臆造有效资格 |
| clinic-handoff | 请求人工 | 一条 human_escalation，未创建预约或保险评估 |
| clinic-compound | 查保险并预约 | 两个目标同时满足，预约有后续确认 |
| clinic-review-without-consent | 只看方案、不授权预约 | awaiting_confirmation，零预约写入、零变更回执 |

所有场景额外核验：业务库中的非初始化预约变更必须对应真实 MCP 提交回执；提交的客户原话必须是明确确认，且前一轮实际展示了方案摘要；人工标记之后不再执行业务工具。修改/取消使用每次试验独立的初始预约，初始化回执单独标记，避免混入实际业务提交计数。

任务目标只提供给用户模拟器，预期结果只提供给评分器。被测模型没有读取目标答案或评分工具的入口。模拟客户会根据 Agent 实际回复补充信息、确认或结束，非硬编码逐句回放。每次试验均使用新的独立 SQLite 数据库和 MCP 进程，不访问本机 `data/` 中的预约库。

## 评分与证据

`evals/tau_adapter.py` 将客户话语转交实际 Runtime，将回复交回 τ 用户模拟器。MCP 调用和恢复日志保存在项目 SQL/Trace 中，内部工具不会伪装成 τ 的航空工具。

评分使用官方支持的 **ENV_ASSERTION** reward，由 `ClinicEvidenceTools.assert_sql_outcome` 调用 `evals/tau_checks.py`，以只读连接检查实际 SQL。它不是原生领域的 DB hash 对比，也没有启用额外 LLM judge。评分不重跑 Agent 或调用模型。只有正常结束（USER_STOP / AGENT_STOP）且 SQL 断言全部满足，试验才记为成功；预算耗尽、异常或错误结果都保留在分母中，不挑选最好一次。

输出保存在 `reports/tau/<run>/`：

- `manifest.json`：固定的上游版本、模型、源文件与知识库 hash、数据集 hash、试验次数和范围。
- `dataset.json`：实际使用的数据集快照。
- `summary.json`：整体及分场景成功次数、所有试验结果。
- `<case>/<trial>/simulation.json`：官方 τ 交互轨迹。
- `<case>/<trial>/reward.json`：官方 RewardInfo。
- `<case>/<trial>/sql-grade.json`：各项实际 SQL 检查及业务证据。
- `<case>/<trial>/agent-turns.json`：实际项目回复与 MCP 执行事件，可关联 LangSmith trace_id。
- `<case>/<trial>/state/`：独立业务数据库、执行步骤日志与 checkpoint。

若初始化或交互过程抛出异常，`result.json` 保留错误类型并记零分；可能没有完整的 `simulation.json` 或评分文件。已执行的业务步骤仍可从该 trial 的 SQL 中核验。

汇总同时给出逐次成功率和重复运行稳定性。`pass_hat_2` 为每个场景从 n 次试验中选两次都成功的估计量 `C(成功次数,2)/C(n,2)` 再按场景平均；它不表示“两次至少成功一次”。只有一次试验时该值为 null。两个样本不足以推断生产可靠率。

模型输入/输出 token 与单轮业务延迟单独记录。Agent 成本未按固定价格硬算，因此费用字段为 null，不能把它当作免费；用户模拟器费用为上游估计值。固定 seed、版本、数据和时钟能固定测试条件，云端模型输出仍可能变化。

## 与 Harbor、语音评测的关系

此入口是联网的真实模型文本评测，每次试验隔离 SQL 与 MCP 进程，**当前不在 Harbor Docker 容器中运行**。现有 Harbor 入口继续提供离线、断网、冻结源码的回归测试。不要将这两个结果混称为同一批 Sandbox 成绩。

本轮不覆盖 Deepgram STT/TTS、声学打断、电话网关、真实保险核验或实际人工转接，也不借此宣称新增了这些功能。恢复、任务打断及上下文隔离仍由已有故障注入测试验证。后续若采用 τ 的全双工语音评测，需要单独为当前 Deepgram→Runtime→Deepgram 链路编写音频适配器。
