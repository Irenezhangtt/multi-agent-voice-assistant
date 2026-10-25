# Deepgram 托管 STT/TTS 部署与联调

当前 MVP 面向英文电话。使用 Deepgram 的独立 STT/TTS WebSocket API，不使用 Deepgram Voice Agent API。Claude Supervisor 通过 LangGraph ReAct 自主委派三个独立子 Agent；子 Agent 通过 MCP 自主选取工具；预约直接使用本地 SQL，保险使用演示规则 RAG，转人工记录 human escalation。

```text
浏览器麦克风 → AudioWorklet → PCM16 / 16kHz / mono
 → 本地 Python WebSocket → Deepgram Nova-3 STT
 → 句末转写 → Claude Supervisor → ReAct 子 Agent → MCP → 本地 SQL / 保险 RAG
 → 英文业务回复 → Deepgram Aura-2 TTS → PCM 音频流 → 浏览器播放
                             ↘ LangSmith traces（配置后启用）
```

## 1. 配置

在 [Deepgram Console](https://console.deepgram.com/) 创建 API Key，并填写本机 `.env`。不需要 Azure 资源、Region 或公网回调地址；Python 主动建立到 Deepgram 的出站 WSS 连接。网络需允许访问 `api.deepgram.com:443`。

```dotenv
AGENT_MODE=claude
ANTHROPIC_API_KEY=你的ClaudeKey
ANTHROPIC_MODEL=claude-sonnet-4-6
DEEPGRAM_API_KEY=你的DeepgramKey
DEEPGRAM_STT_MODEL=nova-3
DEEPGRAM_TTS_MODEL=aura-2-thalia-en
SPEECH_LANGUAGE=en
SPEECH_ENDPOINTING_MS=650
```

Key 仅留在服务端 `.env`，不要发送到聊天或前端。Claude 与 Deepgram 分别计费。`AGENT_MODE=mock` 可单独验证语音接口，但其规则解析仅支持有限演示表达。没有 Key 时主页会显示未配置，不能开始通话。

本版本使用英文业务回复模板。STT 的语言支持和 TTS 的语言支持不同；不能仅修改 `SPEECH_LANGUAGE` 就获得中文语音回复。模型与声音的可用性请核对官方文档。

## 2. 安装和启动

```bash
bash scripts/setup.sh
.venv/bin/python -m voice_agent doctor
bash scripts/start.sh
```

打开 <http://127.0.0.1:8765>，点击“开始语音”，自行允许浏览器麦克风权限。修改 `.env` 后重启服务。Doctor 仅显示配置状态，不验证云端认证。项目使用 Python `websockets` 连接 Deepgram，无需安装或运行本地语音模型。

这是浏览器麦克风语音入口；真实电话号码尚未接入。本机测试无需公网 API。以后接入电话平台仍需按平台要求提供媒体入口、鉴权和格式适配。远程浏览器麦克风需要 HTTPS。

## 3. 英文语音验收

使用虚构信息并戴耳机：

1. 说 “I would like to book a cleaning.”，检查英文追问。
2. 提供虚构电话、姓名和未来日期、时间，听完整摘要后说 “Confirm booking.”，核对唯一 SQL 预约编号。
3. 播放期间讲话或点击“停止回复”，检查旧声音停止，新话语可继续处理。
4. 说 “Check my insurance. My insurer is Aetna and my member ID is DEMO123 for cleaning.”，检查明确说明模拟资格查询不保证承保。
5. 说 “I want to speak to a human.”，应播报已记录 human escalation，没有真实接通人工。
6. 结束通话后检查麦克风释放、连接清理及本地 trace。

未配置真实 Deepgram Key 时，以上云端识别、发音、延迟和声学打断均尚未验收。

## 4. 轮次、打断和状态

- STT 中间结果只用于显示和打断；`is_final` 提交的是片段，直到 `speech_final` 或 `UtteranceEnd` 才合并成业务轮次，避免片段重复触发预约。默认 endpointing 为 650ms，另用 1000ms UtteranceEnd 检测；真实体验需联调。
- TTS 在完整业务回复后发送 `Speak`、`Flush`，逐块接收 PCM，收到 `Flushed` 后完成；不是 LLM token 级输出。每轮独立 TTS 连接。
- 本地 VAD 发现讲话即停止播放；服务端取消 TTS，尽力发送 `Clear` 并关闭连接，按 generation 丢弃旧音频。浏览器负责回声消除。
- 打断停止声音，不撤回已提交的业务写入。已开始的业务动作继续保存结果，后续语音按序处理。
- 每通电话只允许一个语音连接；连接期间禁止同时提交文字请求。队列有限，连接最多 20 分钟；识别故障会结束音频会话，不伪装成功。
- 每次开始语音创建新电话记录，跨电话业务恢复尚未实现。
- 原始音频不持久化。转写和英文回复进入业务 trace；`voice_events` 记录供应商、事件、音频字节数、耗时和业务 trace_id。LangSmith 云端需另行配置。
- Harbor/Sandbox 是离线评测环境，使用本地 SQL、离线演示模型和固定案例，不消耗真实 STT/TTS；音频质量验收单独进行。更换代码后需创建新的 Harbor 导出快照，不能复用旧版本快照。

## 5. 本地 WebSocket 协议与测试

路径 `/api/calls/{call_id}/voice`。上行是 raw little-endian PCM16、16000Hz、单声道，建议每帧 20ms / 640 字节，最大 32000 字节；也支持 JSON `interrupt`、`stop`、`ping`。下行 JSON 包含 `ready`、`transcript`、`status`、`turn_result`、`audio_start`、`audio`（base64 PCM，含 generation）、`audio_end`、`interrupt`、`error`。服务端检查 Origin，仅监听本机；尚不是带用户认证的公网服务。

```bash
.venv/bin/python -m pytest -q
node tests/check_audio_worklet.cjs
.venv/bin/python -m voice_agent eval
```

测试用模拟 WebSocket 验证 Deepgram 协议、PCM、片段聚合、取消清理、异常脱敏，并用模拟供应商验证完整语音协调器与业务数据库。离线测试不证明真实云端效果。

官方接口：[流式 STT](https://developers.deepgram.com/reference/speech-to-text/listen-streaming)、[流式 TTS](https://developers.deepgram.com/reference/text-to-speech/speak-streaming)、[TTS 模型和语言](https://developers.deepgram.com/docs/tts-models)。
