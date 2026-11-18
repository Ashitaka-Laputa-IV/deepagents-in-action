# AgentSeek 准备篇（上）：用生命周期工作流启动 DeepAgents 模板

> 一句话总结：用 AgentSeek 的 `create`、`info`、`task`、`doctor`、`dev` 五类生命周期命令，把 `deepagents/research` 模板从零启动为可运行的研究应用，并留下可供下一章分析的 LangSmith Trace。

## 1. 这一章解决什么问题

后续章节需要一个真实可改、可调试的 DeepAgents 研究应用。若从零手工搭建，学员容易把时间耗在 Python/uv/Node 版本、依赖安装、模型 Key、端口与启动顺序上，这些问题与 Deep Agents 本身无关。

AgentSeek 的定位是“模板 + 生命周期工具”：它为项目生成骨架，再读取项目内的 `.agentseek/lifecycle.toml`，据此检查工具、环境变量、一次性任务和本地服务。它不接管应用自己的框架代码，因此生成的项目仍是普通的 DeepAgents 工程。

完成本章后应得到四样东西：可编辑的 `research_deepagent` 项目、由 TOML 声明的开发流程、可用 `doctor` 检查并由 `dev` 启动的前后端，以及可选的 Trace。

## 2. 核心概念与机制

生命周期被拆成五个阶段，每个阶段只回答一个问题：

| 阶段 | 命令 | 回答的问题 |
|---|---|---|
| 创建 | `agentseek create` | 项目从哪里来 |
| 查看 | `agentseek info` | 入口、环境要求和任务是什么 |
| 准备 | `agentseek task` | 依赖装好了吗 |
| 检查 | `agentseek doctor` | 文件、工具、依赖、变量齐了吗 |
| 运行 | `agentseek dev` | 本地服务怎么起 |

`.agentseek/lifecycle.toml` 是这套流程的核心：它把“这个项目需要哪些工具、变量、任务和服务”写成声明，CLI 只负责读取并执行。这样同一组命令可以服务不同模板；代价是任务名、端口和启动命令都可能随模板变化，不能把某一版输出当成永久契约。

两个容易忽略的设计点：

1. **模板来源决定可复现性。** 默认创建使用 CLI 锁定的模板目录；`--checkout main` 则在创建时读取模板仓库的 `main` 分支。后者适合跟随最新模板，但 `main` 会移动，不是版本号。要复现某次结果，应换成当时记录的完整提交 SHA。
2. **任务名属于模板配置，不是 CLI 常量。** 本章模板列出 `sync`（`uv sync`）和 `frontend`（`npm install --prefix frontend`）；名称变化时以 `agentseek task --list` 为准。

模型配置的关键是四项彼此匹配：`AGENTSEEK_MODEL_PROVIDER` 选适配器，`AGENTSEEK_MODEL` 是服务商真实存在的完整模型 ID，`OPENAI_API_BASE` 是请求地址，`OPENAI_API_KEY` 是该地址的凭证。SiliconFlow 提供 OpenAI 兼容接口，所以 provider 仍写 `openai`，凭证仍放 `OPENAI_API_KEY`。

服务拓扑由模板声明：研究图 API 默认 `http://127.0.0.1:2024`，React 前端默认 `http://127.0.0.1:5174`。后端启动器可能随模板变化（复核时已是 `uv run agentseek-api dev`），权威来源始终是 `.agentseek/lifecycle.toml`。

## 3. 代码与配置要点

安装并确认 CLI：

```bash
uv tool install --upgrade agentseek
agentseek version       # 本文验证时为 AGENTSEEK v0.1.2
agentseek --help
```

创建项目时显式选择模板来源：

```bash
agentseek create --list-templates --checkout main
agentseek create deepagents/research --checkout main --no-input
cd research_deepagent
```

安装后端与前端依赖：

```bash
agentseek task sync
agentseek task frontend
```

配置模型与搜索服务（`.env`，占位符需替换）：

```dotenv
AGENTSEEK_MODEL_PROVIDER=openai
AGENTSEEK_MODEL=zai-org/GLM-5.2
OPENAI_API_BASE=https://api.siliconflow.cn/v1
OPENAI_API_KEY=<your-siliconflow-api-key>

TAVILY_API_KEY=<your-tavily-api-key>

LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
```

检查与启动分两个终端：

```bash
agentseek doctor          # 静态检查
agentseek dev --dry-run   # 只预览启动计划
agentseek dev             # 终端 A，保持运行
agentseek doctor --live   # 终端 B，检查声明的本地服务
```

浏览器打开 `http://127.0.0.1:5174`，用公开问题验证：

```text
Research what LangGraph 1.0 added vs 0.x. Cite sources.
```

若要在下一章分析 Trace，在 `.env` 中改为 `LANGSMITH_TRACING=true` 并填入 `LANGSMITH_API_KEY`、`LANGSMITH_PROJECT=deepagents-course`；LangChain/LangGraph 会自动埋点，不需要改应用代码。

## 4. 实验观察（若有 notebook）

本章无配套 notebook，以下为按章节实测记录整理的实践建议。

- 章节记录的真实运行使用 SiliconFlow + `zai-org/GLM-5.2`，完成 4/4 个任务，约 473.5 秒。这个数字只证明 5–10 分钟级流程走通，不能当性能基线。
- 先跑 `doctor`，再跑 `doctor --live`，最后才用真实问题验收。三者证据不同：静态检查通过不代表 Key 有效，服务可达不代表模型支持 Tool Call。
- 深度研究期间保持 `agentseek dev` 与页面开启；页面生成 thread URL 后可用于重开会话。
- 运行期间 Trace 会先于最终报告出现，等报告生成后再看完整耗时。
- 结束后在运行 `dev` 的终端按 `Ctrl+C`，确认前后端都停止。

## 5. 常见误区

- **把 `--checkout main` 当版本号。** 它是分支引用，随时移动；复现要记提交 SHA。
- **自造环境变量名。** 模板只声明 `OPENAI_API_KEY` 等字段；写成 `SILICONFLOW_API_KEY`、`GLM_API_KEY` 不会被读取。
- **把 API Base 当代理。** `OPENAI_API_BASE` 是模型服务地址；`HTTP_PROXY`/`HTTPS_PROXY` 才是网络转发；`UV_INDEX_URL` 只改包下载源。
- **把 Key 写进命令行。** 命令与 `--api-key` 参数可能进入 Shell 历史、进程列表或助手日志。
- **用 UI 面板判断成败。** v0.7 不默认提供 `write_todos`，未显式传入 `TodoListMiddleware` 时没有计划面板；子 Agent 卡片取决于模型是否选择委派。
- **在 Trace 未完成时读耗时。** 运行中的 Trace 会持续增长，结论应在报告结束后再下。

## 6. 自测清单

- [ ] 我能说出 `create`、`info`、`task`、`doctor`、`dev` 各解决哪一个问题。
- [ ] 我知道 `.agentseek/lifecycle.toml` 是任务与服务的权威来源，并能用 `agentseek task --list` 核对任务名。
- [ ] 我能解释为什么 SiliconFlow 场景仍使用 `OPENAI_API_KEY`，且 `OPENAI_API_BASE` 不是代理。
- [ ] 我能在两个终端分别完成 `agentseek dev` 与 `agentseek doctor --live`，并用真实问题得到带来源的报告。
- [ ] 我知道 `--checkout main` 不可复现，且不会把真实 Key 写进命令或提交到 Git。
