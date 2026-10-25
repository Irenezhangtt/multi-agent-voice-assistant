# 并行子 Agent 与双向流式语音（v6）

Supervisor 的 Claude 工具绑定允许一次规划多个委派。独立的 Booking 和 Insurance 工具调用在两个线程中同时执行；每个子 Agent 仍是独立的 LangGraph ReAct 图，内部具有步骤依赖的模型／工具循环保持按序。

分支只读取批次开始时的上下文快照；每个分支拥有独立 ExecutionControl 和 step_key，避免并发修改同一个步骤标识。SQL 日志、任务 revision 和写入栅栏继续共享。返回后在锁内合并自己的 booking 或 insurance 领域，保留兄弟分支的结果，更新 Supervisor 的可信事实。预约需要确认时，其状态优先于保险已完成状态。

当 `fact_keys` 包含同批次另一领域时，调度器认为存在依赖，并先执行先决任务，即使模型返回的列表顺序相反。循环依赖不会启动业务操作。客户明确要求人工时，handoff 优先，当前批次其他业务委派不启动。条件式需求仍依赖 Supervisor 正确表达依赖，应在数据集中持续覆盖。

## 流式边界

- **STT**：原本已通过 Deepgram WebSocket 持续上传 PCM，返回 interim／segment 识别。用户开始说话立即打断旧任务和音频；完整话语由 speech_final／UtteranceEnd 提交。临时识别可能改变，不会每个词都执行预约工具。
- **回复文本**：业务回复主要来自已核验的 MCP 回执和规范预约摘要，不是任意模型自由生成。v6 在一个子 Agent 产生可用工具结果时即发布，不必等待兄弟 Agent 或额外 Supervisor 总结。模型推理、未完成工具参数和未经核实的成功声明不进入 TTS。这不是把模型的所有 token 直接播出。
- **TTS**：`synthesize_stream()` 在同一 WebSocket 上同时发送增量文本和接收 PCM。首句尽早发送 Speak／Flush；剩余文本持续 Speak，文本流结束时再 Flush，避免每个 token／句子都强制 flush。支持 Clear、取消和 generation 检查。
- **播放**：浏览器收到第一段音频即排入播放器，STT 仍持续工作；不等待整个音频文件下载。

Deepgram 接口依据：[连续文本流](https://developers.deepgram.com/reference/text-to-speech/speak-streaming)、[Flush 控制与限制](https://developers.deepgram.com/docs/tts-ws-flush)。当前保持 Aura v1 的现有声音模型配置。

## 提前播报与恢复

提前播报预约方案前，系统在同一 SQL 事务内检查 execution revision，保存已核验的方案、规范回复和 history，并记录 `published:reply`。客户在保险尚未完成时立即确认，也能找到上一条已发布的方案；客户新话语使旧任务失效，旧分支不得追加写入或发布结果。

SQL grader 从确认任务持久化的 base 状态验证上一条方案，因此能够识别“旧复合任务尚未整体结束，但方案已发布且客户确认”的路径。完整业务 turn 与 trace 仍在整轮结束后保存。恢复可能重述已发布的结果；不会把未核验模型文本当作完成回执。

语音事件新增 `reply_chunk`、`first_audio`。`stt_final_to_first_audio_ms` 衡量服务端完整转写提交至首段音频发送的时间，包含排队与处理；它不包含识别出最终话语前的等待，也不等于客户设备实际播放延迟。恢复任务以恢复开始时间作为起点。

## 验证结果

89 项本地测试通过，覆盖真正并行、跨域依赖排序、人工优先、并发步骤日志、提前播报后立即确认、旧任务中断写入栅栏，以及首段音频在文本流结束前返回。旧恢复、MCP 权限、SQL 断言及语音回归继续通过。

真实 API 对照使用同一条演示请求，临时 SQL，不写本机客户库：预约方案加独立未知保险查询，不授权创建预约。旧源码固定在上一轮 Harbor 的快照，新源码为 v6；二者均使用真实 Claude、stdio MCP 和 Deepgram TTS。结果见 `reports/latency/parallel-v6/report.md`。

| 指标 | 旧版 | v6 |
|---|---:|---:|
| 模型调用次数 | 6 | 4 |
| 首段服务端 TTS 音频 | 25.02 秒 | 7.67 秒 |
| 完整业务结果 | 24.65 秒 | 11.22 秒 |

这是一组单次对照，模型响应和网络有波动，不能据此估计生产 P95。输入是文本，不含 STT 断句时间、麦克风或客户端播放缓冲；7.67 秒也仍偏高。后续应依据实际模型子 span 分析，评估更快的规划模型、角色输出预算及减少单任务不必要的回合。

本机服务版本应显示 workflow=`parallel-streaming-v6`、prompt=`parallel-prompts-v6`。旧电话绑定 v5，需要新建电话。代码入口为 `voice_agent/multi_agent.py`、`voice_agent/execution.py`、`voice_agent/voice.py` 和 `voice_agent/speech.py`。
