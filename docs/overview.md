# Voice Agent 当前架构

默认已升级为 Supervisor + 三个独立 ReAct 子 Agent + MCP Toolkit；以 [多 Agent 说明](multi-agent.md) 为当前架构依据。旧固定 workflow 留作显式基线与规则回归。

| 文件或目录 | 职责 |
|---|---|
| `voice_agent/api.py` | FastAPI、音频 WebSocket、电话记录与本地 SQL 查询入口 |
| `voice_agent/speech.py` | Deepgram STT/TTS 的出站 WebSocket 适配 |
| `voice_agent/voice.py` | 轮次队列、打断、音频生成编号、断线清理 |
| `voice_agent/router.py` | 固定 workflow 模式的路由与离线解析；多 Agent 由 Supervisor 自主委派 |
| `voice_agent/graph.py` | 旧固定 workflow 基线 |
| `voice_agent/multi_agent.py` | Supervisor 与独立子 Agent 的 LangGraph ReAct 循环、委派和独立上下文 |
| `voice_agent/execution.py` | SQL 执行步骤日志、恢复、任务版本与打断检查 |
| `voice_agent/context.py` | 可信事实和子 Agent 的最小上下文 |
| `voice_agent/prompts/` | 四个角色各自的 system prompt |
| `voice_agent/mcp_client.py` / `mcp_server.py` | 真实 MCP stdio 连接、工具发现与调用 |
| `voice_agent/mcp_policy.py` | 按角色限制工具白名单 |
| `voice_agent/workflows/` | 预约确认与变更、保险判断入口、人工升级 |
| `voice_agent/appointments.py` | SQL 预约事务、客户去重、改期、取消、操作回执 |
| `voice_agent/identity.py` | 电话号码规范化，生成客户 ID |
| `voice_agent/insurance_rag.py` | 按元数据检索证据、单次 LLM 判断、引用检查与存档 |
| `voice_agent/knowledge/` | 版本化的虚构保险规则与参保记录 |
| `voice_agent/storage.py` | 数据库表、旧库迁移、对话和 trace 存储 |
| `voice_agent/runtime.py` | 组装模型、图、业务后端、checkpoint 和逐轮执行 |
| `voice_agent/tools.py` | LangChain Tools 封装 |
| `voice_agent/static/` | 浏览器麦克风入口与文字调试页 |
| `evals/` | 24 个案例、grader、runner、Harbor 导出与 verifier |
| `tests/` | 单元与集成回归、SQL 并发/迁移、RAG 边界、语音协议 |
| `scripts/` | 安装、启动和 Harbor 运行脚本 |
| `data/` | 本地 SQL 与 checkpoint，不进入 Git |
| `reports/` | 本地评测与隔离运行证据，不进入 Git |

## 预约执行

收集电话号码 → 查询该客户有效预约 → 新增或变更候选 → 校验时段 → 展示摘要 → 明确确认 → SQL 事务 → 核验幂等操作回执 → 返回 ID 和状态。

同一电话号码最多一条有效预约，`confirmed` 和 `modified` 为有效状态；`cancel` 保留记录但释放客户与时段唯一约束。改期沿用 ID，revision 递增。`booking_mutations` 记录前后快照与操作 ID。手机号本身不证明身份，本地 MVP 仅用虚构数据。

## 保险执行

收集 payer、member_id、service → 检索匹配 policy 与 enrollment 文档 → Claude 结构化判断 → 校验引用及证据 → 保存结果。

当前检索是按结构化元数据过滤的小型本地 RAG，不需要额外向量数据库或 embedding API。`active` 表示演示证据支持，不是向真实保险公司核验；资料不足返回 `unknown`。未来可替换资料和检索器，保留判断与证据契约。

## 人工执行

保存原因、预约信息、保险状态和近期对话摘要 → 标记 `human_escalation` → 停止本通电话的自动操作。没有真实转接。

## 本地与评测边界

服务为单进程 FastAPI + SQLite，只监听本机，当前无需云部署。修改系统版本后旧电话不继续执行，应新建电话。浏览器语音与真正的 PSTN 电话入口分开，后者尚未接入。

Harbor 离线回归每个案例独立容器、独立数据库，固定输入和时钟，使用脚本模型边界。另有真实对话证据回放轨道，在 Docker verifier 内调用独立 Claude Judge，并把评分同步到 LangSmith；见 [Harbor Judge](harbor-judge.md)。当前没有生产 trace 自动整理标注为 dataset、自动优化或完整音频 benchmark。v6 的并行和双向流式设计见 [说明](parallel-streaming.md)。公开聚合结果见 [results](results/README.md)，原始报告保留在本机 `reports/`。
