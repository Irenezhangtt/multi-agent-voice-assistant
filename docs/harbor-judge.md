# Harbor Sandbox 内的独立 Judge 与业务指标

业务 Agent 使用 Claude；Judge 也可以使用 Claude，但采用 `evals/prompts/clinic-judge.md` 的独立评测提示词和全新的上下文，在 Harbor 创建的 Docker verifier 进程中运行。Judge 只获得待评测的对话、工具事件与 SQL 证据，不共享业务 Agent 的运行内存，也没有预约写入工具。无需再部署一个有业务工具权限的 ReAct Agent 来评分。

当前实现的是**真实对话证据回放评测**：业务 Agent 已在此前真实运行，保存了 Claude／MCP／SQL 轨迹；Harbor 冻结这些输入，在每个 Sandbox 中重新读取 SQL、调用 Judge、上传并回读 LangSmith 评分。这不是在 Sandbox 中重新发起整通业务对话。之前的离线脚本模型回归仍保持独立，不能替代本次 Judge，也不能当作真实模型成绩。

流程如下：

```text
历史真实业务对话 → 冻结 transcript / MCP / SQL / 输入 hash
               → Harbor：每个 trial 一个 Docker Sandbox
                   ├─ replay 阶段：验证并记录证据来源，不调用业务模型
                   └─ verifier 阶段：重新核验 SQL → 独立 Claude Judge
                                      → reward / 质量报告 / LangSmith feedback 回读
               → 汇总业务指标和质量指标
```

Harbor 的 verifier 与 agent 阶段允许单独配置环境变量；本实现只在 verifier 执行时注入 Claude／LangSmith 凭据。源码和输入镜像没有复制 `.env`。容器具有进程及文件系统隔离，允许联网访问托管模型和 LangSmith，并不是断网 Sandbox。依据为当前安装 Harbor 的 `VerifierConfig.env` 及官方 [Task 配置](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/tasks/index.mdx)。

## 本机运行

需要已启动 Docker 和已有 `.harbor-venv`。保留当前 `.env` 的 Claude、workspace 与 LangSmith 配置即可。默认独立 Judge 使用当前 `ANTHROPIC_MODEL`；可选增加 `JUDGE_PROVIDER=anthropic`、`JUDGE_MODEL=账号可用的其他Claude模型`。主业务模型不随 `JUDGE_MODEL` 改变。

```bash
# 冻结输入和源码，输出目录必须是新目录。
bash scripts/harbor-judge.sh prepare --output reports/harbor/my-judge-run

# 构建镜像，并由 Harbor 在 Docker 中执行 verifier。
bash scripts/harbor-judge.sh run --output reports/harbor/my-judge-run

# 从实际 verifier 产物汇总，不用宿主机重新评分。
.venv/bin/python -m evals.harbor_judge_report --export reports/harbor/my-judge-run
```

默认输入为 `reports/judge/clinic-final-cloud/summary.json` 所引用的 19 通真实对话；可用 `--source` 选择具有相同 artifacts／manifest 结构的 cohort。`--limit 2` 适合先做接通验证。每个任务一次、并发两条。重复运行会创建新的 Harbor job 并重新调用付费 Judge；报告默认取最新 job，也可用 `--job` 明确选择。

冻结目录的 `manifest.json` 保存源码及输入 hash；`tasks/*/task.toml` 只保存凭据变量模板。`jobs/*/*/verifier/sandbox-result.json` 是容器内结果，包含 hostname、Docker 标记、SQL 指标、逐项评分和 Cloud 回读证明。Judge Cloud trace 的 `execution_environment` 为 `harbor-docker-verifier`。

Harbor reward=1 要求 Judge verdict 为 pass，且 SQL gate 通过。needs_review 对应 reward=0，但业务 SQL 可能正确，因此需要分别报告“质量通过率”“SQL 成功率”“执行错误数”。模型超时或网络错误保留 error，不作为有效质量评分；未产生 verifier 结果的任务单独计为缺失。

## 如何得到截图里的指标

| 指标 | 计算方式与当前条件 |
|---|---|
| 新预约成功率 | SQL 中实际创建且符合目标的通话数 ÷ 需要创建预约的通话数；取消、改期和仅审阅分别统计 |
| 人工升级率 | 有 human_escalation SQL 标记的通话数 ÷ 全部通话数；目前只能称标记率，不能称真实转接接通率 |
| 应升级成功率 | 正确标记升级数 ÷ 测试集中要求升级的通话数；另外统计不应升级却升级的情况 |
| 平均通话时长 | 真实通话结束时间减开始时间，暂停／重连合并同一 call_id；不能由 LLM 打分生成 |
| 用户提前挂断率 | 任务完成前客户主动结束数 ÷ 有完整结束事件的通话数；网络断线、正常结束及未知原因单独列出 |

现有文本模拟中，新预约成功 **5/5=100%**，人工升级标记 **2/19=10.5%**，要求升级的两通均标记，其他 17 通无额外升级。这些结果与任务集配比有关，不能推断生产转人工率。平均文本模拟运行耗时为 **25.53 秒**，不是电话时长；真实通话时长及挂断率缺少事件数据，因此报告显示 N/A。模拟用户 `USER_STOP` 不表示客户提前挂断。

原始 19 通业务指标报告：`reports/metrics/clinic-text-v1/report.md`。本轮完整 Sandbox Judge 报告：`reports/harbor/judge-claude-full-v1/report.md`。

同一 Claude 模型的独立提示词和环境提供执行隔离，但不能消除共同判断偏差；需要通过人工抽检、负例和固定数据集持续校准。未来跨厂商可选接口位于 `evals/judge_providers.py`，仅做过离线接口测试，本轮没有运行跨厂商评分。
