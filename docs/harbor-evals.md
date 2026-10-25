# Harbor + Sandbox 隔离评测

当前默认已升级多 Agent + MCP。专用数据集为 `evals/multi-agent-dataset.json`，4 个案例、每例 3 次、并发 4。最终代码实际运行 12/12 trials 全部通过，0 exceptions；结果位于 `reports/harbor/multi-agent-mcp-v2/jobs/2026-10-24__16-41-00/result.json`。

本页是离线脚本模型业务回归。真实 Claude Judge 在 Sandbox 中运行的入口、回放范围和业务指标报告见 [Harbor Judge](harbor-judge.md)，两者的分数分开统计。

这些测试使用脚本替代模型的决策，真正执行 Supervisor/子 Agent 图、stdio MCP 和 SQL，运行时无网络。它们衡量编排、授权和持久化正确性，不衡量真实模型推理或音频质量。

```bash
HARBOR_DATASET=evals/multi-agent-dataset.json \
HARBOR_EXPORT_DIR=reports/harbor/multi-agent-mcp-v2 \
bash scripts/harbor-eval.sh run
```

导出白名单包含代码、各 Agent 提示词、演示知识库和依赖锁，不带 `.env`、API Key、业务库或录音。修改代码或提示词后必须选择新目录。Docker 需要安装并启动；MCP 本地运行本身不依赖 Docker。

下面是保留的固定 workflow 基线说明。其历史 72/72 结果不能代表当前多 Agent 模型质量。

Harbor + Sandbox 是本项目的正式评测组件，与 LangChain、LangGraph、LangSmith 一起保留。在线服务和离线评测使用同一套业务图，但运行入口和环境分开。

```text
在线：客户音频 → 托管 STT → Claude / LangGraph → 业务工具 → 回复文字 → 托管 TTS
                                     ↓
                         LangSmith Trace / 业务结果
                                     ↓ 整理、标注、脱敏
离线：版本化 Dataset → Harbor Job → 多个独立 Sandbox Trials
                                      ↓
                       同一套 Graph + 本地 SQL / 离线 RAG + 干净业务库
                                      ↓
                         独立 Verifier → 分数、报告和证据
```

## 当前实现

- 官方 Harbor 0.24.0 安装在独立 `.harbor-venv`，完整依赖锁在 `requirements-harbor.lock`，不改变在线服务的依赖环境。
- `evals/harbor_export.py` 将 24 个现有案例导出为标准 Harbor task 目录。
- 每个 task 包含 `instruction.md`、`task.toml`、环境 Dockerfile、调用者输入和独立 `tests/test.sh`。
- `evals/harbor_agent.py` 是 BaseInstalledAgent 适配器；应用代码在 sandbox 内运行，不在 Harbor 宿主进程上执行业务。
- 默认 4 个并发、每例重复 3 次：24 × 3 = 72 个 trials。
- 每个 trial 使用独立容器和全新业务数据库、checkpoint。多轮对话共享同一个 trial 内的状态；下一次 trial 重新初始化。
- 运行与验证阶段配置 `network_mode = "no-network"`。Docker 镜像构建阶段需要拉基础镜像和 Python 包。
- 初期评测是固定时间、固定输入、本地 SQL / 离线 RAG的文字业务回归。未实现真实 Claude、Deepgram STT/TTS 或整通电话的 Harbor 测评。

**验证边界：** 46 个 Python 测试与 24 个离线案例通过。Docker 引擎现已就绪，24 个 task 和 Job 配置通过 Harbor 官方 schema，72 个真实容器 trials 已完成，72/72 reward=1、0 exceptions；结果见 `docs/verification.md` 和本地 jobs 目录。

## 准备任务

从项目根目录运行：

```bash
bash scripts/harbor-eval.sh prepare
```

输出默认在 `reports/harbor/local-sql-rag-v5/`：

```text
manifest.json                 快照 ID、源文件 hash、数据集 hash、所有导出文件 hash
job.json                      并发、重复次数、sandbox 和 task 配置
harbor_clinic_adapter.py       冻结的宿主适配器
tasks/<case-id>/
  instruction.md
  task.toml
  environment/
    Dockerfile
    .dockerignore
    runtime-requirements.txt
    source/                   明确允许的 Python 源文件与依赖锁
    case.json                 输入、故障、固定时间，不包含预期结果
  tests/
    expected.json             预期结果，仅供 verifier 使用
    test.sh
```

导出过程不复制 `.env`、API Keys、录音、业务库、虚拟环境或生产日志。它冻结当前磁盘代码，包括尚未提交的修改，而不仅仅依赖 Git commit。导出后会校验内容：相同内容可复用；内容变化或被添加意外文件时拒绝覆盖。改动代码后选择新目录，例如：

```bash
HARBOR_EXPORT_DIR=reports/harbor/candidate-v2 bash scripts/harbor-eval.sh prepare
```

## 安装 Harbor

本机已完成。其他环境需要 Python 3.12+：

```bash
python3.12 -m venv .harbor-venv
.harbor-venv/bin/python -m pip install -r requirements-harbor.lock
```

## 运行真正的隔离批量评测

先安装并启动 Docker，确认 `docker info` 正常。然后：

```bash
bash scripts/harbor-eval.sh run
```

脚本检查 Docker CLI 和 daemon；缺少时明确退出，不退回宿主机执行，也不宣称已经完成沙箱评测。没有创建云账号、使用云 Sandbox 或发布数据集。

也可以直接运行已冻结的配置：

```bash
PYTHONPATH="$PWD/reports/harbor/local-sql-rag-v5" \
  .harbor-venv/bin/harbor run -c reports/harbor/local-sql-rag-v5/job.json
```

结果放在导出目录的 `jobs/` 下。Harbor 收集每个 trial 的 Agent 日志、数据库、执行记录、verifier 报告和数值 reward。`1` 表示案例满足断言，`0` 表示失败；基础设施异常由 Harbor 单独记录，不当作模型质量分数。

## 如何评分与避免虚假成功

Agent 执行脚本调用原有 `AgentRuntime` 和三个 workflow，并保存每轮响应、业务结果和 trace。Verifier 在执行完成后读取预期结果，用已有 grader 评估各轮记录，并以只读方式再次查询最终 SQLite 业务库，核验最终结果是否与报告一致。

缺失证据、案例或快照不匹配、最终数据库与报告不符都会得到 0 分。Verifier 输出 `/logs/verifier/reward.txt` 和详细的 `report.json`。Agent 输出在 `/logs/agent/` 下。

当前适用于受控业务 agent 的回归测试，不是针对恶意代码 agent 的防评分作弊环境。历史轮次检查使用保存的快照，最终业务状态有独立数据库核验。

## 可复现的含义

本版固定测试输入、时间、离线演示返回和故障配置，绑定代码、grader、依赖锁、数据集和基础镜像引用。每次运行的 UUID、记录时间和性能耗时仍会不同；比较业务行为与断言结果，不要求二进制文件逐字相同。

默认基础镜像使用 `python:3.12.14-slim` 标签。严格归档需要在可运行 Docker 的环境中解析并记录镜像 digest，再使用固定引用导出：

```bash
.venv/bin/python -m evals.harbor_export \
  --output reports/harbor/pinned-image \
  --base-image 'python@sha256:替换为实际镜像digest'
```

真实 Claude 或音频服务涉及模型非确定性和供应商更新，未来要采用多次重复、明确的供应商模型配置及统计比较，不能承诺完全确定的重放。

## LangSmith 与后续扩展

当前离线 trial 禁网并关闭 LangSmith 云上传，保留本地 traces 和版本。后续由宿主机在评测结束后导出选定的脱敏结果，或在接入托管 Sandbox 时使用经过验证的 Harbor/LangSmith 集成。当前没有实现报告自动上传。

后续分两条评测轨道：

1. 确定性回归：Mock Claude/语音与业务工具，验证路由、状态、参数和业务结果。
2. 真实模型/音频集成：合成或脱敏音频、Deepgram 测试凭据、Claude、受限网络白名单；验证识别、延迟、轮次及打断。此轨道待实现。

三条 workflow 各有案例，还要保留跨流程切换与端到端案例。Sandbox 按 trial 隔离，而不是仅给每个 workflow 一个长期共用环境。

参考：[Harbor task 格式](https://docs.harborframework.com/tasks/overview)、[自定义 Agent](https://docs.harborframework.com/agents/custom-agents)、[批量 Job](https://docs.harborframework.com/jobs/run-a-job)。
