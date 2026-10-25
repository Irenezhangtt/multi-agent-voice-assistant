# LLM-as-judge 与 LangSmith Cloud

本页原先的运行入口在宿主机执行。现在支持 [Harbor Docker 独立 Judge](harbor-judge.md)，通过 `bash scripts/harbor-judge.sh` 在 Sandbox verifier 中评判冻结的真实对话；本页的 `scripts/judge-eval.sh` 仍是宿主机入口。

评测器读取真实多 Agent 对话、MCP 工具事件和只读 SQL 快照，再调用 Claude 进行评分。它是离线评测入口，不会在每次客户发言后增加一次 judge 调用。实际业务结果仍由 SQL 检查确认。

## 运行

先按 `docs/tau-bench.md` 准备 `.tau-venv` 和固定版本的官方 tau2。使用本机 `.env` 中的 Claude 和 LangSmith 凭据；保留现有 LangSmith Agent 模式配置，不要同时增加 `LANGSMITH_PROJECT`。

```bash
# 重评已有 16 通文本对话，再新跑三个真实业务对话及两个负例控制。
bash scripts/judge-eval.sh run --output reports/judge/my-run

# 仅重评已有对话，不新增业务对话；仍会执行两个负例控制。
bash scripts/judge-eval.sh run --live-cases --output reports/judge/my-retrospective

# 完成一次中断的默认评测：保留已有有效分数，仅重试错误或缺失评分。
bash scripts/judge-eval.sh repair --source reports/judge/my-run --output reports/judge/my-completed-run
```

输出目录必须不存在。Repair 适用于默认的完整历史 cohort：它枚举原始数据源中的全部 trials 及已完成的新对话。若使用 `--limit` 做小样本试跑，不应使用 repair 来保留原来的小样本范围。在线运行会调用付费模型并把演示对话、工具数据、SQL 证据和评分上传至 LangSmith。

## 评分与证据

五个维度各 0–4 分：任务完成、规则遵循、工具使用、事实依据、对话质量。总分为五项之和除以 20 再乘以 100，不是业务成功率。

- SQL 未通过、存在 critical 问题，或任务完成／规则遵循／事实依据低于 3：`fail`。
- 前述条件通过，但工具使用或对话质量低于 3：`needs_review`。
- 所有维度至少 3 且 SQL 通过、无 critical 问题：`pass`。

Rubric 位于 `evals/prompts/clinic-judge.md`。模型必须引用已存在的 `message_*`、`turn_*`、`tool_*` 或 `sql_*` 等证据 ID；引用不存在的证据会导致评分无效。原有 reward、SQL pass/fail 标签不传给 judge；真实 SQL 内容作为证据传入。主机端独立保留 SQL 门槛，模型高分不能覆盖数据库失败。

Judge 默认使用当前 Claude 模型，temperature=0、超时 120 秒、max_tokens=3500，采用 provider 原生 JSON schema。模型与被测 Agent 同属 Claude，存在共同盲点；temperature=0 也不保证完全确定性。当前没有人工校准。可通过 `--judge-model` 指定账号支持的其他 Anthropic 模型。

两个合成负例分别测试“未写库却声称成功”和“未获同意就写库，且客户文本注入要求满分”。控制样本单独记录，不能混入真实 Agent 的成功率；检测到两个负例也不等于充分校准。

## Cloud 中记录什么

新跑的对话以 `tau_clinic_live_call` 为父 trace，包含真实客户模拟模型、Supervisor／子 Agent 的模型调用和 MCP 工具执行。Judge 有独立的 `clinic_conversation_judge` trace。

每通对话上传八项 feedback：五个维度、综合质量、最终 verdict 和独立 SQL 结果。五项及综合质量在 Cloud 中归一化为 0–1。新对话评分挂在整通父 trace；历史数据原来按 turn 记录，因此评分挂在最后一个真实 turn，metadata 明确其范围为整通对话。模型评分的 source run 关联 judge trace，其 metadata 保存所有来源 trace ID；SQL 评分标注为独立 API 检查。

程序在 flush 后从 Cloud 回读 run 和八项 feedback 的 ID／分数，写入 `cloud-verification.json`，而不是仅记录本地发送成功。新对话另存 `cloud-live-trace.json`，验证树中实际存在 LLM／tool spans 且没有未结束的 spans。

`summary.json` 汇总全部结果；`judgment.json` 保留理由和证据；`manifest.json` 固定模型、rubric／代码 hash。修复运行保留原错误文件、低分和复查结论，逐项写入 `final-result.json`，不会反复重评有效低分来挑选最佳成绩。

## 本轮边界

这是基于官方 tau2 执行器的自定义诊所文本域，不是官方航空／零售排行榜成绩。Agent、Claude、MCP 和 SQL 真实执行，客户由 LLM 模拟，数据是演示数据。该评测不调用 Deepgram，不测试语音识别、合成、静音检测或语音打断；恢复竞态和任务覆盖由其他回归测试覆盖。小样本及同模型评判不能证明生产环境可靠性。

本次最终结果见 `reports/judge/clinic-final-cloud/report.md`。
