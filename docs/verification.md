# 多 Agent 本地验证记录

验证日期：2026-10-24。当前默认架构：Supervisor + Booking/Insurance/Handoff 独立 ReAct Agents + stdio MCP Toolkit。

| 检查 | 结果 |
|---|---|
| Python 回归 | 全量 56 项通过；新增 Supervisor 无工具不得宣称预约成功的保护项亦通过，当前共 57 项。多 Agent/MCP 专项 10 项通过 |
| 固定 workflow 基线 | 24/24 离线案例通过；保留作 SQL/规则回归，不代表多 Agent 推理质量 |
| 多 Agent 专用离线案例 | 4/4；脚本模型决策，真实 Supervisor/子 Agent 图、MCP stdio、SQL |
| 最终 Harbor Docker | 4 案例 × 3 次，12/12 reward=1，0 exceptions；每个容器独立 SQL 证据已保存 |
| 真实 Claude 预约 | Supervisor 委派 Booking Agent，经 MCP 准备方案；下一轮客户确认后由 MCP 提交，临时 SQL 中只有 1 条预约 |
| 真实 Claude 复合请求 | Supervisor 先调 Insurance Agent：MCP 检索→Agent 判断→MCP 保存，再调 Booking Agent 准备方案；状态和证据共享成功，未确认不写预约 |
| API 账户 | Workspace 已配置，充值后的 Claude 调用已成功；此前额度不足为历史诊断 |
| 本地服务 | 127.0.0.1:8765，architecture=multi-agent；/api/agents 验证 MCP connected=true |
| 音频采集 | AudioWorklet 16kHz / 44.1kHz / 48kHz → 单声道 PCM16 16kHz 通过；Deepgram 接口保留 |
| LangSmith | 主图、子 Agent、委派、模型与主机 MCP 工具可追踪；未独立读取云端列表确认上传 |

真实调用仅使用虚构姓名、电话号码和保险资料，并使用临时数据库，不修改用户预约库。演示保险有效不等于真实保险公司核验或赔付承诺。真实通话的语音识别、声学打断和整体体验仍需人工验收。

本次真实 Claude 测试中，预约两轮分别约 7.6s 和 6.0s，保险→预约复合请求约 19.1s，包含多次串行模型调用；这是单次样本，不是延迟保证。

报告：

- `reports/multi-agent-live.json`：真实两轮预约路径与 MCP 调用证据。
- `reports/multi-agent-compound-live.json`：真实复合委派、保险引用和预约准备。
- `reports/harbor/multi-agent-mcp-v2/jobs/2026-10-24__16-41-00/result.json`：最终代码的 12 个隔离 trials。
- `reports/harbor/local-sql-rag-v5/`：历史旧 workflow 的 72 个隔离 trials，不能替代本轮多 Agent 测试。

多 Agent/MCP 权限、同轮确认拦截、最近助手回复与方案绑定、操作回执恢复、知识检索前置、共享上下文、循环预算及人工升级终止已覆盖自动化回归。


## 可恢复执行与上下文隔离（2026-10-24）

- 67 项本地回归通过，覆盖过期任务写入拦截、旧模型未返回时新任务完成、模型超时、跨 Runtime/MCP 重启、语音重连和上下文隔离。音频工作线程采样验证与 JavaScript 语法检查通过。
- `reports/durable-live.json`：真实 Claude + 实际 MCP + 临时 SQL，准备预约与下一轮确认成功，仅一条预约记录。
- `reports/durable-recovery-live.json`：真实 Claude 在子 Agent 工具步骤完成后模拟打断；关闭并新建 Runtime/MCP 进程后恢复，已保存 MCP 步骤没有重复执行；下一轮确认只执行 commit，SQL 仍为一条记录。
- `reports/harbor/durable-isolated-v5-final/`：最终源码冻结后的四类多 Agent 场景，每个运行三次，12/12 通过，零异常；使用脚本模型、真实 stdio MCP 和 SQL。它测业务路径的隔离回归，不替代上述恢复竞态测试，也不衡量真实模型推理质量。

语音协调器以模拟 Deepgram 边界验证了重连和最新话语替换；本次没有自动开启真实麦克风或拨打电话。服务已重启为 `durable-supervisor-v5` / `isolated-prompts-v5` / `fenced-mcp-v3`，保留本机数据；旧版本电话需要新建。


## τ-bench 自定义诊所域（2026-10-24）

- 已固定官方 tau2 2.0.0 / 0c69df456a96aa76a1162591574dad9c427a34d7，使用官方文本 Orchestrator、真实模型 UserSimulator 与 EnvironmentEvaluator。
- 8 类自定义诊所任务 × 2 次，16/16 成功；8/8 类两次均成功，零执行异常。SQL 条件、确认与回执、原预约 ID、未授权不写入均核验通过。
- `reports/tau/clinic-v1-live/report.md` 为可读报告，`summary.json` 为汇总，各 trial 保留轨迹、SQL、评分和真实工具事件。代码与数据集 hash 已核对。
- 这是自定义业务域文本评测，不是官方航空/零售任务得分；不包含真实语音，也未在 Harbor Docker 内执行。仅两个重复样本不足以推断生产可靠性。
- 运行方式及评分边界见 `docs/tau-bench.md`。新增 SQL/τ 接入回归也覆盖错误目标不能通过，以及业务目标已达到但交互预算耗尽仍记失败。

## LLM-as-judge 与 Cloud 回读（2026-10-24，本机时间）

- 对此前 16 通真实文本对话和新跑 3 通对话评分：19/19 SQL 通过；judge 平均 92.1/100，13 pass、6 needs_review、0 fail。
- 五项各 0–4 分，对话质量均分 2.79，为最弱项。主要问题是重复能力菜单、重复询问已提供的信息和内部状态提示直接面向客户；业务 SQL 成功不代表对话质量通过。
- 两个独立合成负例均被 judge 判为 fail，分别为虚构写入成功及未获授权写入并带有评分注入。未做人工校准，judge 与 Agent 使用同一 Claude 模型。
- 新跑三通对话的 Cloud 树分别含 98／35／45 spans，包含真实 LLM、MCP 工具及客户模拟模型；19 通评分均从 Cloud 回读八项 feedback。详见 `reports/judge/clinic-final-cloud/`。
- 完整本地回归 77 passed；本次未执行 Deepgram 音频评测或真实 PSTN 电话。
- 原始无效评分／超时记录保留；最终保留 10 个有效评分（包括低分），只重新完成 9 个缺失或无效评分。运行入口与证据边界见 `docs/llm-judge.md`。

## Harbor Docker 内的独立 Claude Judge（2026-10-24，本机时间）

- `reports/harbor/judge-claude-full-v1/`：Harbor 实际执行 19 个 Docker verifier，19/19 有结果及容器标记，零 exceptions、零评分执行错误。运行耗时 6 分 40 秒。
- 真实业务对话证据回放：SQL 目标 19/19 通过；Judge 平均 92.9/100，16 pass、3 needs_review、0 fail；Harbor reward 均值 16/19=0.842，代表质量门槛通过率，不等于 SQL 成功率。
- 19 通各八条 feedback，合计 152 条均在上传后从 LangSmith Cloud 回读确认；独立 Judge 使用专用提示词及 Docker 执行环境，trace metadata 标注 `harbor-docker-verifier`。
- 新预约成功 5/5，人工升级标记 2/19；应升级的两通均标记，其余 17 通未额外升级。缺少真实通话起止与挂断原因事件，平均真实通话时长／挂断率均为 N/A。
- 同证据对比此前宿主评分，5 通总分变化、3 通 verdict 变化；业务 Agent 没有改进，因此不能将分数上升解释为性能提升。报告保留评分波动及输出方式变化说明。
- 冻结镜像／任务及完整 Harbor 产物扫描未发现本机 API Key 内容。凭据仅运行时注入 verifier；允许联网访问模型及 LangSmith。
- 这是在 Sandbox 中重新评判此前已执行的真实文本对话，不是重新在 Sandbox 中运行 19 通业务对话，也不覆盖 Deepgram 音频。运行与指标口径见 `docs/harbor-judge.md`。
- 完整本地回归 83 passed；新增覆盖指标分母、缺失电话事件、运行时凭据模板、独立模型接口错误，以及 Harbor 长任务名称截断后的正确评分对比。

## 并行编排与双向流式 TTS v6（2026-10-24，本机时间）

- 完整回归 89 passed。新增测试证明独立子 Agent 真正并行、交叉事实依赖排序、handoff 优先、执行步骤 key 隔离，以及部分预约摘要发布后立即确认仍能正确写入且旧保险任务无法继续写入。
- 语音与 Deepgram 边界测试证明：音频在后续文本到达前返回；同一条 TTS WebSocket 接收增量已核验文本，首句 Flush，剩余文本在结束时再次 Flush；取消仍发送 Clear，旧 generation 音频不再播放。
- `reports/harbor/parallel-streaming-v6/jobs/2026-10-24__23-05-45/`：冻结最终源码的 12 个 Docker trials，12/12 通过、0 exceptions，使用脚本模型、实际 MCP／SQL；独立并行竞态另外由新增本地测试验证。
- `reports/latency/parallel-v6/`：冻结旧源码与 v6 各执行一次相同请求，真实 Claude／MCP／Deepgram TTS、临时 SQL。模型调用 6→4；首段服务端音频 25.02→7.67 秒；完整业务结果 24.65→11.22 秒。候选 Cloud 树含 4 个 LLM span、5 个 tool span，parallel_dispatch 已核对。
- 此延迟对照以文本输入开始，不含 STT 或客户端播放缓冲；单次样本不足以估计生产 P95。没有自动开启麦克风或扬声器，也未创建真实预约。旧源码、候选源码 hash 与 Cloud 链接均留存；应用源码与最终 Harbor 快照一致。
- 服务已重启为 parallel-streaming-v6／parallel-prompts-v6，旧电话需新建。实现和语音指标边界见 `docs/parallel-streaming.md`。
