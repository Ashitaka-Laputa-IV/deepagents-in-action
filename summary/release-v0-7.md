# Deep Agents v0.7：更轻、更透明、更可配置的 Harness

> 一句话总结：v0.7 把默认层做薄——基础提示词清空、Todo 改为按需、工具说明精简，同时开放 Middleware 原位替换并增强文件工具；代价是应用必须显式接回原先依赖的默认策略。

## 1. 这一章解决什么问题

一个已在线运行的 Deep Agent 升级到 v0.7 后，账单可能下降也可能不变；可能稳定完成任务，也可能突然不再生成 Todo。原因是 v0.7 重新划定了“Harness 替应用做多少决定”。

本章面向两类读者：新读者可直接以最新 0.7.x 开始；已有 0.5/0.6 项目的读者需要先做迁移检查，识别隐式依赖的默认值、被消费的文件工具原始输出和 Backend 兼容接口，再用业务评测验证行为没有退化。

## 2. 核心概念与机制

四组表面变化指向同一方向：默认层更薄，策略由应用显式选择。

| 变化 | 设计判断 | 应用要接手 |
|---|---|---|
| 基础提示词变空、工具说明缩短 | 现代模型能从工具 Schema 理解接口 | 写清业务提示词，用 Trace 查冲突 |
| Todo 不再默认启用 | 显式计划不是普遍增益 | 按任务长度、模型能力、UI 需求决定 |
| 同名 Middleware 可原位替换 | 默认值合理，但不应封死阈值与模型 | 管理替换实例的完整配置 |
| 文件工具更强、更有界 | 需处理大文件大目录，又要避免无限搜索 | 重审删除、覆盖、解析、权限、截断 |

### 2.1 两个 Token 数字的范围不同

- 默认 Agent 的工具 Schema 描述从 **4,005** 降到 **2,302** Token（-43%），其中 `task` 说明从 1,664 降到 389。
- 加上空基础提示词与 Todo 可选，一个默认 Agent 的简单回合输入从 **5,395** 降到 **1,895** Token（约 -65%）。

这只说明“每轮固定携带的框架成本”下降，不能推导“任意应用总成本降 65%”。真实调用还含用户消息、历史、Skills/Memory、工具结果、子 Agent 轨迹、摘要与重试、提供商缓存与计费。

官方跨版本评测为 36 个任务 × 3 次 rollout × 4 个模型。`gpt-5.6-luna` Token -35.5%、成本 -15.2%；`claude-sonnet-4-6` Token +31.3%、成本 +36.8%（两个高难自治任务轨迹变长）；所有模型 Reward 置信区间都跨零，所以结论是“整体质量没有可测量回退”，不是“质量提升”。

验收要分开记录三类指标：基础开销（简单回合 input tokens）、轨迹效率（轮次/工具调用/子 Agent/重试）、业务结果（真实任务是否通过）。

### 2.2 Todo：规划是策略，不是固定税费

不再默认安装 `TodoListMiddleware` 时，三样东西一起消失：`write_todos` 工具、`todos` state channel、Todo 规划提示词。官方在 GPT-5.6 Terra、Claude Opus 4.8、GLM 5.2 上未观察到统计显著的准确率提升，部分模型 Token 更高。

适用判断：单步问答保持关闭；长程多阶段、易漏步骤的任务启用；较弱模型先做 A/B；UI 要展示进度则启用；后台批处理默认关闭再评测。

继承范围是坑：默认 `general-purpose` 只继承主 Agent 对**默认中间件**的同名覆盖，默认栈没有 Todo，所以不会继承主 Agent 显式传入的实例；声明式 `subagents=[...]` 也要各自在 `middleware` 字段启用。默认隔离模式下各子 Agent 的 `todos` 独立。例外是 OpenAI Codex harness profile，其系统提示词依赖 `write_todos`，会自动保留。

### 2.3 Middleware 原位替换：整实例替换，不是字段合并

v0.7 按 `.name` 匹配：同名实例在原位置换掉默认实例，保留栈的相对顺序；没有同名默认实例则插入核心 Middleware 之后、profile/prompt caching/memory 尾部之前。默认 `general-purpose` 继承主 Agent 对默认实例的覆盖；声明式子 Agent 独立构建栈。

关键陷阱：传入自定义 `SummarizationMiddleware` 或 `FilesystemMiddleware` 后，框架不会把默认实例的 Backend、工具说明、权限状态逐字段补齐。因此要共享同一个 Backend 实例，并把替换实例当完整配置重新审查。`deepagents==0.7.0` 中文件权限由框架通过私有配置注入默认 `FilesystemMiddleware`；若同时使用顶层 `permissions=` 又整体替换文件中间件，不能假定拒绝规则会合并。

### 2.4 文件工具的变化

| 变化 | v0.7 行为 |
|---|---|
| `write_file` | 目标存在时直接完整覆盖 |
| `delete` | 进入默认工具，可递归删除目录 |
| `read_file` | 返回总行数、剩余行数、下一 `offset` |
| `grep`/`glob` | 超时返回已找到结果并标记 `truncated` |
| `grep` 匹配数 | Agent 工具默认最多 1,000，流式消费本地 `rg` |
| 空 `ls`/`glob` | 文本为 `No files found`，不再是 `[]` |
| `read_file` 行号 | 行号后两个空格，不再是固定宽度加 Tab |

推荐语义：完整重写用 `write_file`；局部修改先 `read_file` 再 `edit_file` 精确替换。递归删除是全有或全无：任一后代路径命中拒绝规则，整个删除不执行；符号链接删除链接本身。`grep` 的 1,000 上限是 Agent-facing 默认值，模型可用 `max_count` 调整；直接调 Backend 默认无上限。`context_lines` 在 v0.7.0 只先加入本地 `FilesystemBackend.grep()` 的直接调用接口，不是所有 Backend 和工具的统一参数。

### 2.5 会阻塞升级的兼容变化

- **Backend Factory 被移除。** `create_deep_agent()` 现在接收具体 `BackendProtocol` 实例；`StoreBackend` 必须显式设计 namespace，旧的 assistant-id fallback 已删除。namespace 设计错误可能导致跨用户读取。
- **旧符号与旧参数被删除**：`BackendFactory`、`BACKEND_TYPES`、`FileFormat`、`Unset`、`StateBackend(runtime=...)`、`StoreBackend(runtime=...)`、`ls_info`、`glob_info`、`grep_raw`、纯字符串 `read()`、`SummarizationMiddleware(history_path_prefix=...)`。
- `FilesystemBackend` 与 `LocalShellBackend` 默认 `virtual_mode=True`。
- 新文件 `FileData.content` 为字符串；旧版 `list[str]` 仍可读取，并会在下一次覆盖或编辑时转换。

## 3. 代码与配置要点

安装当前补丁版本并限制 minor：

```bash
uv add --upgrade "deepagents>=0.7,<0.8"
uv run python -c "import deepagents; print(deepagents.__version__)"
```

恢复 Todo：

```python
from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)
```

按名称覆盖摘要阈值（默认约在上下文窗口 85% 触发）：

```python
from deepagents.backends import StateBackend
from deepagents.middleware import SummarizationMiddleware

backend = StateBackend()
agent = create_deep_agent(
    model=model,
    backend=backend,
    middleware=[
        SummarizationMiddleware(
            model=summary_model,
            backend=backend,
            trigger=("fraction", 0.5),
            keep=("messages", 20),
            summary_prompt="总结此前对话，原样保留文件路径、已确认的决定、未完成事项和失败原因。",
        )
    ],
)
```

不需要删除能力时，用 allowlist 收窄内置文件工具：

```python
agent = create_deep_agent(
    model=model,
    backend=backend,
    middleware=[
        FilesystemMiddleware(
            backend=backend,
            tools=["read_file", "ls", "glob", "grep"],
        )
    ],
)
```

`read_file` 是必需能力，不能排除；allowlist 只控制八个内置文件工具，不会删除自定义工具，也不能让 Backend 支持它本来不支持的能力。被排除的工具会从 `ToolNode` 注册表移除，不只是对模型隐藏；该覆盖会传给默认 `general-purpose` 子 Agent，声明式子 Agent 仍需单独限制。

消费 Backend 结果时要显式处理截断：

```python
result = backend.grep("TODO", path="/workspace")

if result.error:
    raise RuntimeError(result.error)

for match in result.matches or []:
    consume(match)

if result.truncated:
    schedule_narrower_search()
```

## 4. 实验观察（若有 notebook）

本章无配套 notebook。实践建议：按七步迁移——记录 v0.6 基线（短任务、长程任务、子 Agent、大文件分页/大目录搜索、权限拒绝各一）→ 隔离环境升级 → 迁移会直接报错的 API → 逐项声明原先依赖的默认策略 → 重写原始输出解析测试 → 在 LangSmith 做同任务 Trace 对比 → 渐进放量并以业务验收决定是否继续。

静态扫描可先跑：

```bash
rg -n 'BackendFactory|BACKEND_TYPES|FileFormat|Unset|history_path_prefix' .
rg -n 'ls_info|glob_info|grep_raw' .
rg -n 'backend\s*=\s*(lambda|[A-Za-z_][A-Za-z0-9_]*_factory)' .
rg -n 'split\("\\t"|cat -n|No files found|write_file' .
```

扫描只找常见旧符号，不能发现对 Todo state、工具存在性或错误语义的隐式依赖。

## 5. 常见误区

- **把“基础输入 -65%”当总成本 -65%。** 历史、工具结果、子 Agent 轨迹才是长任务成本主体。
- **认为 Todo 没用。** 它取决于任务长度、模型强弱与 UI 需求；官方结论只是“没有统计显著提升”。
- **升级只改版本号。** Backend Factory、namespace、文件工具输出语义都会在运行期暴露问题。
- **以为同名 Middleware 会字段级合并。** 是整实例替换。
- **把 `truncated=True` 当失败并丢弃结果。** 它是“部分成功”，应消费已有匹配并安排更窄搜索。
- **把 `ToolMessage.status == "success"` 当搜索完整。** 部分结果也可能是成功消息。
- **依赖 `write_file` 已存在即报错。** v0.7 会直接覆盖。

## 6. 自测清单

- [ ] 我能解释 4,005→2,302 与 5,395→1,895 两组数字各覆盖什么范围。
- [ ] 我知道 Todo 关闭后同时失去 `write_todos`、`todos` state channel 与规划提示词，并能为子 Agent 单独启用。
- [ ] 我能说明同名 Middleware 是整实例替换，以及为何要共享同一个 Backend 实例。
- [ ] 我能列出至少三项会阻塞升级的兼容变化，并写出 `StoreBackend` 的显式 namespace 迁移方向。
- [ ] 我能处理文件工具的覆盖写入、递归删除与 `truncated` 部分结果。
