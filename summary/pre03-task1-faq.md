# 叶静的学习笔记

> 一句话总结：把 Task1 的“跑通”拆成五层可验证证据，并用 20 个问答覆盖首次运行、环境网络、配置追踪、结果成本四类故障的定位顺序。

## 1. 这一章解决什么问题

首次运行 `deepagents/research` 时，故障往往混合出现：可能是 WSL 终端选错，可能是代理拦了本地端口，可能是模型不支持 Tool Call，也可能只是 `.env` 没重启。新手常见的反应是看到最后一屏报错就删锁文件、换包管理器或重装全部工具，结果把可复现的现场破坏掉。

本章按“问题查阅”组织，不要求重装一遍。它先定义什么叫跑通，再按四组问题给出检查顺序，让读者能用最小改动定位根因。

## 2. 核心概念与机制

### 2.1 五层验收：不同检查提供不同证据

| 检查点 | 应留下的证据 |
|---|---|
| 工具可用 | Python、uv、Node.js、npm、AgentSeek 能输出版本 |
| 准备完成 | 依赖与必填配置齐全，`agentseek doctor` 通过 |
| 服务可达 | `agentseek doctor --live` 能访问声明的本地服务 |
| 任务完成 | 真实模型与搜索调用成功，报告完整、来源可打开 |
| 运行可解释 | 能说明模型、调用过程、错误与大致用量 |

这五层不能互相替代。“网页打开了”只是其中一层；`doctor` 通过只说明本地准备条件成立。

### 2.2 组件职责

| 组件 | 职责 |
|---|---|
| AgentSeek | 创建、查看要求、跑准备任务、检查与启动服务 |
| Deep Agents 后端 | 执行模型与工具调用，具体命令由模板声明 |
| React 前端 | 输入任务、查看进度与报告 |
| 模型 API | 提供推理与工具调用 |
| Tavily | 联网搜索，需要独立 Key |
| LangSmith | 可选追踪，观察调用、错误、耗时与用量 |

端口 `2024`/`5174` 是课程模板常用值，不是恒定契约；入口以 `agentseek info` 为准。

### 2.3 排障总原则

1. 先保存最早的完整错误，不要只贴最后一屏连锁报错。
2. 一次只改一个因素，并记录改前改后的结果。
3. 先确认终端与目录（WSL 还是 PowerShell、是否在项目根目录）。
4. 用已知可用配置建立基线，再引入新模型或新代理。
5. 分享日志前脱敏；URL、代理地址、`curl -v` 输出都可能含凭证。

## 3. 代码与配置要点

最小首跑顺序（已有项目直接进入原目录，不要重新生成覆盖）：

```bash
python3 --version && uv --version && node --version && npm --version && git --version

uv tool install agentseek
agentseek create deepagents/research --no-input
cd research_deepagent
agentseek info
agentseek task --list

test -f .env || cp .env.example .env
test -f frontend/.env || cp frontend/.env.example frontend/.env

agentseek task sync
agentseek task frontend
agentseek doctor
agentseek dev --dry-run
agentseek dev
```

另开终端做服务健康检查：

```bash
agentseek doctor --live
```

代理场景要同时同步大写与小写绕过变量，并保留已有名单：

```bash
bypass="${NO_PROXY:+$NO_PROXY,}${no_proxy:+$no_proxy,}127.0.0.1,localhost"
export NO_PROXY="$bypass"
export no_proxy="$bypass"
unset bypass
```

四项模型配置必须互相匹配：

| 配置 | 确认内容 |
|---|---|
| `AGENTSEEK_MODEL_PROVIDER` | 使用哪个适配器 |
| `AGENTSEEK_MODEL` | 服务商是否提供这个完整 ID |
| `OPENAI_API_BASE` | 请求实际发往哪个服务 |
| `OPENAI_API_KEY` | 是否为该服务的有效凭证 |

字段叫 `OPENAI_API_KEY` 不代表必须填 OpenAI Key；SiliconFlow 的 OpenAI 兼容接口同样用它。Tavily Key 与模型 Key 不通用。若模板提供 `AGENTSEEK_PARALLEL_TOOL_CALLS=false`，初次运行应保留课程基线。

检查凭证是否会被提交：

```bash
git check-ignore .env frontend/.env
```

该命令只验证忽略规则，不能证明历史提交没有泄露。

## 4. 实验观察（若有 notebook）

本章无配套 notebook。实践建议：

- 第一次验收用公开、范围小、可核验的问题，例如比较 Python `list` 与 `tuple` 的两个区别，要求中文、附来源、约 300 字。
- 只提一次任务，不要等待时反复点击或并行开多个研究；回答长度是提示，不是调用次数或费用上限。
- 验收看三件事：有真实工具调用证据、报告完整、来源能打开且支持结论。只有流畅文字不算完成。
- 修改 `.env` 后重启进程；`load_dotenv()` 默认不覆盖已有进程环境，“最后一行永远生效”不是跨工具规则。
- LangSmith 无 Trace 时按顺序查：开关与 Key → Endpoint 区域 → 工作区/项目筛选 → 是否重启并产生新调用 → 后端上传错误。
- 成本异常时先停止重复试跑，保留 Trace，再看每次模型调用的输入/输出 Token、重试与重复调用；父节点可能汇总子节点，不要重复相加。

## 5. 常见误区

- **`doctor` 通过就等于任务能跑。** 它不验证 Key 有效性、额度、模型 Tool Call 能力或远端可用性。
- **`curl` 能联网就说明所有工具都能联网。** 不同工具访问不同域名，使用不同代理与证书配置。
- **npm 失败就换 Yarn。** 网络、权限、版本不兼容、依赖冲突需要不同处理，换工具可能引入多套锁文件。
- **改完 `.env` 立即生效。** 变量可能被终端、IDE 或启动配置中的同名值覆盖。
- **报告生成即成功，或 Token 高即 bug。** 两者都要看来源质量、调用轨迹与单价。
- **关闭网页等于停止任务。** 后端可能仍在运行，已处理的请求不一定能撤销或退款。
- **泄露后补 `.gitignore` 就够。** 凭证已进入历史或公开日志时必须撤销并更换。

## 6. 自测清单

- [ ] 我能复述五层验收，并说明 `doctor`、`doctor --live` 与真实任务各证明什么。
- [ ] 我能在一个终端启动 `agentseek dev`，在另一个终端完成 `doctor --live` 与一次小任务。
- [ ] 我能解释 Provider、模型 ID、API Base、Key 四项为何必须匹配，以及 `OPENAI_API_KEY` 的命名含义。
- [ ] 我知道代理绕过要同时设置 `NO_PROXY`/`no_proxy`，且包镜像不等于网络代理。
- [ ] 我能在求助时提供脱敏的环境、命令、预期、最早错误与已尝试的单项改动。
