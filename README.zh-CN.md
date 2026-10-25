# Clinic Voice Agent — Multi-agent + MCP

当前默认架构为 **Supervisor + 三个独立 ReAct 子 Agent + 本地 MCP Toolkit**。Supervisor 自主选择子 Agent；Booking、Insurance、Handoff 分别用自己的提示词和工具循环，接收按需传入的可信事实及独立任务上下文，并自主调用 MCP 工具。预约结果仍写入 SQLite，语音仍使用 Deepgram STT/TTS，模型使用 Claude。

```text
客户语音 → Deepgram STT → Supervisor ReAct Agent
                            ├─ Booking Agent → MCP 预约查询 / 准备 / 确认提交
                            ├─ Insurance Agent → MCP 检索 → 自己判断 → MCP 保存
                            └─ Handoff Agent → MCP human_escalation
                         ↔ Supervisor 可信事实库 / 隔离的子 Agent 上下文
                         → 英文回复 → Deepgram TTS
```

旧的固定 workflow 保留作基线回归，不在多 Agent 失败时偷偷降级。详细架构、独立提示词、工具权限及确认规则见 [多 Agent 说明](docs/multi-agent.md)。本地可用 `GET /api/agents` 查看运行配置。

执行支持持久化步骤恢复；新客户话语替换未完成的旧任务。语音页面可暂停并继续同一电话。见 [恢复、打断与上下文隔离](docs/durable-execution.md)。

v6 增加独立预约／保险子 Agent 的并行调度，以及已核验结果提前进入 Deepgram 双向流式 TTS；STT 持续收音与输出临时识别，话语边界触发任务。依赖任务按序执行，人工升级优先。见 [并行与流式链路](docs/parallel-streaming.md)。升级后刷新语音页面并新建电话。

真实对话的 LLM-as-judge 评测、SQL 校验和 LangSmith Cloud trace／评分回读见 [评测说明](docs/llm-judge.md)。运行入口为 `bash scripts/judge-eval.sh run`，本轮报告位于 `reports/judge/clinic-final-cloud/report.md`。

需要在隔离环境中执行 Judge 时，使用 [Harbor Sandbox Judge 与业务指标](docs/harbor-judge.md)：`bash scripts/harbor-judge.sh run --output reports/harbor/my-judge-run`。它在 Docker verifier 中回放评判真实对话证据，单独统计 SQL 新预约成功率、人工升级标记率和质量分；缺少真实电话事件的通话时长／挂断率显示 N/A。

## 本机启动

```bash
bash scripts/setup.sh
.venv/bin/python -m voice_agent doctor
bash scripts/start.sh
```

打开 <http://127.0.0.1:8765>，点击开始语音并允许麦克风。文字调试：<http://127.0.0.1:8765/debug>；API 文档：<http://127.0.0.1:8765/docs>。

在本机 `.env` 配置（不要把 Key 放进聊天或 Git）：

```dotenv
AGENT_MODE=claude
AGENT_ARCHITECTURE=multi-agent
AGENT_MAX_ROUNDS=5
ANTHROPIC_API_KEY=你的ClaudeKey
ANTHROPIC_MODEL=claude-sonnet-4-6
# 如果 API Key 未绑定单一 workspace，另填：
# ANTHROPIC_WORKSPACE_ID=wrkspc_...
DEEPGRAM_API_KEY=你的DeepgramKey
DEEPGRAM_STT_MODEL=nova-3
DEEPGRAM_TTS_MODEL=aura-2-thalia-en
SPEECH_LANGUAGE=en
```

LangSmith Agent 模式：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=你的LangSmithKey
LANGSMITH_AGENT_ID=voice-agent
LANGSMITH_AGENT_ENVIRONMENT=local
```

Agent 模式不要同时设置 `LANGSMITH_PROJECT`。项目模式则只设置 `LANGSMITH_PROJECT`，不设置两个 Agent 变量。Workspace 或区域配置以 LangSmith 页面给出的值为准。Claude workspace ID 来自 Claude Console，不同于 LangSmith workspace ID；未绑定单一 workspace 的 Key 需要它。修改 `.env` 后重启；旧版本电话记录需新建电话继续测试。

## 预约规则

- 询问客户电话，以规范化后的号码作为 `customer_id`。十位号码默认美国/加拿大 `+1`；其他国家显式提供国家码。电话号码识别客户，不等于身份验证。
- 同一客户最多一条有效预约；同一时段最多一条有效预约。有效状态为 `confirmed` 或 `modified`。
- 新增：确认摘要后插入 SQL，状态 `confirmed`。
- 改期：确认后更新原记录的日期/时间，保留预约 ID，状态 `modified`。`booking_mutations` 保留修改前后内容。
- 取消：确认后把原记录状态改为 `cancel`，不删除，释放时段。取消后再预约产生新记录。
- 不同电话使用同一客户号码，可以查询、修改或取消原预约；未知写入结果不会自动重试成另一笔预约。
- 事务、唯一索引、幂等操作回执和 revision 检查共同防止重复写入与旧状态覆盖。
- 当前单诊所单资源，每天 09:00、10:00、14:00、15:00，支持未来 90 天的 cleaning / consultation。

示例（日期需在未来）：

```text
Book a cleaning. My name is Alex. My phone number is 2025550100.
I would like November 3, 2026 at 2 PM.
Confirm booking.
Reschedule my appointment to November 4, 2026 at 3 PM.
Confirm change.
Cancel my appointment.
Confirm cancellation.
```

实际语音日期、姓名和号码识别需联调；Mock 模式只支持有限的中英文规则表达，建议使用 `YYYY-MM-DD HH:MM`。

## 保险 RAG

知识库在 `voice_agent/knowledge/insurance-demo.json`。Insurance Agent 先自主选择 MCP 检索工具，读取匹配的规则与参保资料，再由自己的 Claude 工具循环判断并调用记录工具；返回 `active` / `inactive` / `unknown`、原因和引用。没有匹配参保证据或引用无效时不能确认有效。模型异常明确报告失败，不偷偷退回规则答案。

当前资料全部虚构：Aetna + DEMO123 + cleaning/consultation 为演示有效，EXPIRED 为演示无效；其他会员为未知。这不是实际 Aetna 条款，不构成真实资格或赔付保证。保单规则本身不足以证明某个客户已参保。

可通过 `INSURANCE_KB_PATH` 指定同格式 JSON。替换资料时须同步维护稳定文档 ID、payer、service 规则、匹配客户的参保状态、来源及版本。每次评估保存证据、知识库版本/hash、模型、原因和引用到 `insurance_assessments`，便于审计。Mock 模式执行离线演示判断，不调用 LLM。

## 人工升级

请求人工时保存原因和对话摘要，状态为 `human_escalation`，结束该通电话的自动业务处理。不接通真实电话，不创建回拨任务。

## 查看 SQL 与历史

- `data/business.sqlite3`：预约、操作历史、保险评估、人工标记及本地 trace。
- `data/checkpoints.sqlite3`：LangGraph 状态恢复。
- `GET /api/bookings?customer_phone=2025550100`：本地查询该客户的预约（包含取消记录）。
- `GET /api/bookings/{booking_id}/history`：每次创建、改期、取消的前后快照。

旧预约表自动迁移，保留 `bookings_legacy_v1`；没有电话的旧行使用 `legacy:<booking_id>`，不会按姓名猜测客户归属。迁移前应备份 SQL 数据库。

## 验证与隔离评测

```bash
.venv/bin/python -m pytest -q
node tests/check_audio_worklet.cjs
.venv/bin/python -m voice_agent eval
HARBOR_EXPORT_DIR=reports/harbor/local-sql-rag-v5 bash scripts/harbor-eval.sh prepare
# Docker Desktop 已安装且启动后：
HARBOR_EXPORT_DIR=reports/harbor/local-sql-rag-v5 bash scripts/harbor-eval.sh run
```

旧 workflow 基线已有 24/24 案例与历史 72/72 Docker trials。当前新增多 Agent/MCP 测试和专用 Harbor 场景；真实 Claude 两轮预约以及保险→预约复合调度已成功验证，详见 [验证记录](docs/verification.md)。离线通过率不代表真实模型质量。

24 个离线案例验证业务图和 SQL 结果，每例干净数据库、固定时钟。Harbor 默认 4 并发 × 每例 3 次 = 72 trials；运行时断网，语音和模型使用离线边界。导出包含代码、演示知识库和版本快照，不包含 `.env`、业务库或 Key。改动源代码后必须新建导出目录。

当前服务仅监听 127.0.0.1，适合单人本地开发，不需要公网 API。未接入电话网关；未提供生产认证、多进程并发或多租户隔离。Deepgram 音频事件仍保存在本地，LangSmith 主要追踪业务图、LLM 与工具。语音质量和真实云端接收需要单独验收。

目录与架构见 [overview](docs/overview.md)，语音设置见 [部署说明](docs/voice-deployment.md)，隔离评测见 [Harbor 说明](docs/harbor-evals.md)。


## τ-bench 真实交互评测

已接入官方 τ-bench 文本 Orchestrator、用户模拟器与环境评分器，使用实际 Claude 多 Agent / MCP / SQL。当前是八类自定义诊所任务，不是官方航空或零售排行榜成绩。

```bash
bash scripts/tau-eval.sh setup
bash scripts/tau-eval.sh run --dry-run
bash scripts/tau-eval.sh run --trials 2
```

每次试验创建独立数据库，默认同时调用真实 Claude Agent 和模型客户；报告保存在 `reports/tau/`。详细评分边界、重复试验指标和证据目录见 [τ-bench 接入说明](docs/tau-bench.md)。
