# 可恢复执行、任务打断与上下文隔离

本版默认 Claude / multi-agent 模式实现这三项能力。服务仍按单个本地 FastAPI worker 运行；不支持多个应用进程同时接管同一通电话。

## 执行与恢复

每条最终转写先在 `business.sqlite3` 的 `executions` 中持久化，再进入执行。`execution_steps` 保存 Supervisor 和子 Agent 已完成的模型响应、MCP 工具结果；主 LangGraph checkpoint 继续保留。恢复时固定原始输入、基础状态、提示词版本和时钟，重放已保存步骤以还原运行状态，从第一个未完成步骤继续。不会重新请求已记录的模型或重复调用已记录的工具。

进程突然退出后，进行中的任务仍可通过同一 call_id 恢复。尚未完成或尚未来得及保存响应的模型请求可能重新调用供应商；不能保证每个网络请求恰好执行一次。预约 SQL 回执、人工记录唯一键和保险评估事务内回执防止已提交操作重复写入。保险的检索证据也保存至 SQL，MCP 子进程重启后仍能继续核验引用。

浏览器保存当前 call_id，点击“暂停通话”后可点击“开始 / 继续语音”；刷新页面后也沿用该 call_id。重新连接语音 WebSocket 后，服务自动恢复最新的待处理任务并合成恢复结果。已完成的业务状态通过原电话记录保留；系统不保存原始音频，也不从某个音频采样点续播。选择“新建通话”会让后续语音创建新 call_id。

API：

- `GET /api/calls/{call_id}`：查看业务状态和 `execution`；后者为待执行/运行/暂停任务，没有待办时为 null。
- `POST /api/calls/{call_id}/suspend`：暂停未完成任务。
- `POST /api/calls/{call_id}/resume`：恢复同一电话的最新未完成任务；语音连接期间由 WebSocket 自动恢复。
- `POST /api/calls/{call_id}/turns`：提交新话语，覆盖该电话尚未完成的旧任务。

更新代码后的旧版本电话需要新建；本版上线之前没有步骤日志的历史调用，无法追溯恢复其内部执行点。保留本机 `data/` 中的 SQL 文件才能恢复，同一 call_id 必须使用相同系统版本。

## 新消息覆盖旧任务

开始检测到客户说话或收到显式 interrupt 时，停止当前 TTS，并暂停该电话的业务执行。STT 最终转写到达后递增电话任务 revision，将旧任务标记 superseded，并只保留最新的一个待处理任务。部分转写用于显示和打断，不作为可执行业务请求；最终完整转写才发给编排器。旧任务不能恢复为当前任务。

业务线程在模型等待、工具调用、保存结果和最终回复提交时检查 revision。模型等待不再阻塞最新任务；已发给供应商的同步请求可能仍在后台完成并计费，但其迟到结果不会执行工具或发布回复。MCP 工具的 SQL 事务在 `BEGIN IMMEDIATE` 后再次检查任务版本。打断与提交由数据库事务确定先后：打断生效之后旧任务不能新增写入；先前已提交的写入保留，通过 SQL 回执核验。已提交预约不会因为客户开始说话自动取消。

新版替换未完成任务的输入，保留上一轮已保存的业务槽位和 SQL 事实；不把未完成旧任务的完整聊天或推理灌入新任务。若新话语只有“改到三点”而相关预约信息尚未被保存，Agent 需要继续询问缺失字段。

## 子 Agent 的上下文隔离

Supervisor 持有整个电话的规范业务状态、最近对话和所有已核验事实。工具返回的 patch 由主机验证后合并；SQL 查询用于核对进程中断前可能已提交的结果。客户身份字段和待确认方案单独作为输入，手机号不等于身份已核验。

每次子 Agent 委派只接收：

1. 有长度限制的 delegated_task 和最新客户原话。
2. 自己领域的 pending_task，含尚未完成的槽位、待确认方案。
3. Supervisor 按角色权限选择的 trusted_facts，含来源及精简字段；不含检索原文和别的 Agent 消息。
4. 固定诊所时钟和时区。

Booking 默认读取 booking 事实，Insurance 默认读取 insurance 事实；Handoff 默认读取 booking 和 insurance 摘要。Supervisor 可用 `delegate_*(request, fact_keys=[...])` 按需提供跨领域事实。Booking/Insurance 可选 booking 和 insurance；Handoff 还可选 handoff。工具范围不能由模型扩展。

子 Agent 运行期间拥有自己的 System/Human/AI/Tool 消息列表。结束后仅返回精简 summary、status、fact_delta，不把内部模型/工具消息回传给下一个子 Agent。可信本地主机向 MCP Server 注入的权限校验 scope 仍包含必要的规范状态；这个隐藏 scope 不会发送给子 Agent 模型。Trace 中可记录各 Agent 的调用，用于调试，不等于将这些消息放进运行上下文。

## 验证范围

`tests/test_execution.py` 覆盖模型未返回时新任务完成、SQL 事务内拒绝过期写入、跨 Runtime/MCP 重启重放、预约提交后丢失 RPC 响应、保险检索后恢复，以及事实选择和消息隔离。`tests/test_voice.py` 覆盖同一电话语音重连自动恢复、最新排队话语替换和音频中断。测试使用脚本模型与模拟语音供应商边界，MCP stdio、SQLite 和 WebSocket 协调器实际运行；它们不代表真实云端识别准确率。
