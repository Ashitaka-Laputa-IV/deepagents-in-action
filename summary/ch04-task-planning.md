# 第 4 章：任务规划与分解 — 让 Agent 学会拆解复杂任务

> 一句话总结：用 `TodoListMiddleware` 按需为 Agent 装上 `write_todos`，把复杂任务拆成可追踪的清单；同时理解清单只是进度标记，交付物仍要独立验收。

## 1. 这一章解决什么问题

简单任务可以一步完成，复杂任务却要搜索多个来源、整理资料、对比分析、组织结构、撰写报告。没有明确清单时，Agent 容易遗漏步骤、重复搜索、上下文变长后失去主线，或者质量忽好忽坏。

本章给出两件事：一是 v0.7 中如何**显式启用**任务规划；二是揭开引擎盖，说明 `write_todos` 只是 LangChain `TodoListMiddleware` 注入的一个工具，从而理解 Deep Agents 的中间件装配方式。后半部分还划清了规划、上下文压缩、消息修补、状态持久化各自的职责边界。

## 2. 核心概念与机制

### 2.1 规划能力是“按需脚手架”

`TodoListMiddleware` 同时注入三样东西：

1. `write_todos` 工具；
2. Agent State 中的 `todos` 字段；
3. 引导模型先规划再执行的提示词。

v0.7 默认不安装它。是否启用应由任务决定：

| 场景 | 建议 |
|---|---|
| 单步问答、短工具调用 | 关闭，避免计划比任务还长 |
| 长程、多阶段、容易漏步骤 | 启用，并用真实任务检查完成率与轨迹长度 |
| 模型较弱、容易跑偏 | 先做 A/B 评测，通常值得尝试 |
| UI 需要展示计划与进度 | 启用，`todos` 同时是产品状态协议 |

### 2.2 任务结构与状态

每个任务只有两个字段：

```python
{"content": "搜索 LangGraph 官方文档，整理核心架构和 API 设计", "status": "pending"}
```

| 状态 | 含义 | 不能据此推断 |
|---|---|---|
| `pending` | 待办 | 工具已排队自动执行 |
| `in_progress` | 进行中 | 一定有后台任务在跑 |
| `completed` | Agent 标记完成 | 新增需求也完成、结果一定正确 |

常见流转是 `pending → in_progress → completed`，但这不是框架强制的状态机；模型可以根据新信息增删、重排任务。

### 2.3 清单是“整表替换”，不是增量合并

这是本章最容易踩的语义：`write_todos` 每次提交的列表会**完整替换**旧清单，不按 `content` 自动匹配合并。要保留的任务必须一起提交；省略一项不等于把它标记完成，而是把它从清单里删掉。

清单保存在 Agent State 的 `todos` 字段，与 `messages` 分开。默认对话摘要不会删除 `todos`；但跨多次 `invoke()` 自动接续需要 Checkpointer 并复用同一 `thread_id`。`InMemorySaver` 只在当前进程内保存检查点，进程重启后要恢复需改用数据库等持久化 Checkpointer；部署到 Agent Server 时由平台提供持久化能力。

子 Agent 默认不继承主 Agent 的 `TodoListMiddleware`：默认 `general-purpose` 和 `subagents=[...]` 声明的子 Agent 都需要在自己的 `middleware` 中启用；隔离模式下，子 Agent 的清单也不会读写主 Agent 的 `todos`。

### 2.4 中间件：能力的插件机制

`create_deep_agent()` 的本质是把一组中间件自动组装到 Agent 上。Hook 分两类：

| 风格 | Hook | 执行方式 | 适合 |
|---|---|---|---|
| Node-style | `before_agent`、`before_model`、`after_model`、`after_agent` | 编译成图中独立节点，按生命周期运行 | 校验、状态更新、审计、人工中断 |
| Wrap-style | `wrap_model_call`、`wrap_tool_call` | 包裹一次调用，可调用 `handler` 零次、一次或多次 | 重试、缓存、降级、请求/响应转换 |

这个区别对 `interrupt()` 很重要：Node-style 有清晰节点边界，恢复时更容易推断哪些逻辑会重放；Wrap-style 位于 model/tools 节点内部，恢复时可能连同 `handler` 一起重新执行，不适合作为默认中断边界。

v0.7 的装配分三类：

- **默认**：`FilesystemMiddleware`、`SummarizationMiddleware`、`PatchToolCallsMiddleware`，以及由模型 profile 决定的 prompt caching 等。
- **专用参数**：`subagents=` → `SubAgentMiddleware`；`skills=` → `SkillsMiddleware`；异步子 Agent → `AsyncSubAgentMiddleware`；`memory=` → `MemoryMiddleware`；`interrupt_on=` → `HumanInTheLoopMiddleware`。
- **`middleware=[...]`**：`TodoListMiddleware` 等可选策略。与默认中间件同名的实例会**原位置换**，且是完整实例替换，不会按字段合并新旧配置。

### 2.5 几个容易混淆的职责

| 问题 | 处理者 | 边界 |
|---|---|---|
| 有工具调用但缺响应 | `PatchToolCallsMiddleware` | 只补一条说明消息，不重试工具 |
| 工具遇到可重试异常 | 应用显式配置的 `ToolRetryMiddleware` | 写操作要考虑重复执行 |
| 下次运行接续上次状态 | Checkpointer + 同一 `thread_id` | 跨进程需持久化 Checkpointer |
| 结果是否正确 | 应用校验、评测或人工审核 | 调用成功、清单完成都不等于正确 |

`PatchToolCallsMiddleware` 在 `before_agent` 检查历史，为缺失响应的调用补 `ToolMessage`，说明调用被取消或未能执行。它不重新执行搜索，也不证明外部操作已经撤销。

### 2.6 两个同名 `SummarizationMiddleware`

| 对比项 | LangChain 版本 | Deep Agents 版本 |
|---|---|---|
| 导入 | `langchain.agents.middleware` | `deepagents.middleware` |
| 位置 | `before_model`，独立图节点 | `wrap_model_call`，模型节点内部 |
| 摘要后 | 替换 `state["messages"]` 中的旧消息 | 保留原始消息，另记摘要与截断位置 |
| 历史保存 | 不负责写 Backend | 旧消息写入 Backend，摘要附路径 |

因此 `agent.get_graph()` 里能看到 LangChain 版的摘要节点，看不到 Deep Agents 版的摘要节点。Deep Agents 通过 `request.override(messages=...)` 调整本次输入；要按路径读回历史，还需配置使用同一 Backend 的 `FilesystemMiddleware`。提供 `compact_conversation` 工具的是另一个 `SummarizationToolMiddleware`。

手动实例化两个同名类时**必须设置 `trigger`**，默认值都是 `None`，不会主动摘要；`create_deep_agent()` 才会为默认摘要选择触发条件。Deep Agents 还会在模型抛 `ContextOverflowError` 时摘要后重试，这与按 `trigger` 主动摘要是两条路径。

## 3. 代码与配置要点

显式启用规划：

```python
from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

agent = create_deep_agent(
    model=model,
    middleware=[TodoListMiddleware()],
)
```

`TodoListMiddleware` 只有两个可选参数：`system_prompt`（规划指导）和 `tool_description`（工具描述）。多数情况默认即可，只有在需要特别引导（如“先写测试再写代码”）时才改。

在更底层的 `create_agent()` 上手动组装：

```python
agent = create_agent(
    model=model,
    tools=[],
    middleware=[
        TodoListMiddleware(),
        FilesystemMiddleware(),   # 注入 read_file / write_file 等
    ],
)
```

规划 + 文件 + 摘要共用同一个 Backend，摘要里的历史路径才能被读回：

```python
backend = StateBackend()
agent = create_agent(
    model=model,
    tools=[],
    middleware=[
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend),
        SummarizationMiddleware(
            model=model,
            backend=backend,
            trigger=("tokens", 4000),
            keep=("messages", 20),
        ),
    ],
)
```

`trigger` 是何时开始摘要，`keep` 是保留多少近期消息；比例写法 `("fraction", 0.85)` 需要模型 profile 提供 `max_input_tokens`，窗口未知时用 `("tokens", N)` 或 `("messages", N)`。这里的 4,000 / 20 只是示例。

完整研究任务的骨架：

```python
agent = create_deep_agent(
    model=model,
    tools=[internet_search],
    middleware=[TodoListMiddleware()],
    system_prompt="""你是一位专业的技术研究员。
面对复杂研究任务时，你会：
1. 先用 write_todos 制定研究计划
2. 逐步执行每个步骤，及时更新进度
3. 将搜索结果写入文件系统整理
4. 最终输出完整的研究报告
""",
)
```

系统提示词里的四步是**期望行为**，不是固定执行顺序；运行后要检查实际工具调用、`result.get("todos", [])` 和最终产物是否一致。

## 4. 实验观察（若有 notebook）

Notebook 以 offline 脚本模型运行，工具与框架真实执行，但计划与报告文本是预设的；第 1 节验证配置，第 2–4 节跑完整两轮案例，第 6、8 节用脚本模型做机制对照。live 已验证 `Qwen/Qwen3-Coder-30B-A3B-Instruct` 于 2026-10-22 整本通过，但不要求相同计划措辞或状态流转。

### 4.1 启用与未启用的硬证据

不调用模型，只组装两个 Agent 并检查工具注册表和输出 Schema：

```text
未启用：write_todos 已注册 = False ；todos 字段已声明 = False
已启用：write_todos 已注册 = True ；todos 字段已声明 = True
```

未启用时模型仍能用文字拆步骤、调用其他工具，只是没有内置的 Todo 记录机制；启用也不会自动完成任务。

### 4.2 两轮需求变化：从两维初稿到三维修订

案例要求比较 Deep Agents、Claude Agent SDK、Codex SDK 的定位与工具/文件能力，提交初稿；随后用户追加“接入方式”维度。第二轮先调用 `review_report` 检查旧稿，返回 6 条缺项：

```text
Deep Agents 缺少 integration（接入方式）
Deep Agents 需要本轮补查取得的来源
Claude Agent SDK 缺少 integration（接入方式）
...（三个对象各两条）
```

这说明验收合同变了：旧稿在两维合同下可以通过，在新合同下必须补查并重新提交。offline 轨迹随后把清单从 4 项扩展到 8 项（3 个补查 + 1 个提交修订报告），再执行 3 次搜索、提交三维修订报告。实际搜索共 6 次：initial 3 次、expanded 3 次，每次 1 条结果。

### 4.3 整表替换与四个对照

小对照先写 3 项，第二次只提交 1 项，最终 `todos` 就只剩这 1 项：

```text
原清单：核查 Deep Agents / 核查 Claude Agent SDK / 核查 Codex SDK
只提交一项后：核查 Claude Agent SDK（in_progress）
```

省略第三项表示它从清单中消失，不表示它已完成。

四个受控对照进一步说明“验收看产物，不看声明”：

| 对照 | 结果 |
|---|---|
| 全部 `completed`，但没有实际资料 | 失败：没有足够的实际搜索调用证据 |
| 全部 `completed`，但报告缺少新维度 | 失败：最后提交的报告未通过 |
| 没有 `write_todos`，但报告与来源齐全 | 通过 |
| 另一份计划仍 `in_progress`，但报告与来源齐全 | 通过 |

notebook 还提醒：`recursion_limit=60` 限制图执行步数，不是模型与搜索费用上限；`InMemorySaver` 只保证当前进程内的线程接续，不等于跨进程长期记忆。

## 5. 常见误区

- **加了中间件就以为模型一定会规划**。工具存在只代表“可调用”，是否调用由模型、提示词和任务复杂度决定。
- **把 `completed` 当验收结论**。它只是 Agent 写入的进度标记，报告是否生成、来源能否核实、代码是否通过测试都要另行检查。
- **以为清单会自动跨 `invoke()` 接续**。没有 Checkpointer 和同一 `thread_id`，第二次调用看不到上一次的 `todos`。
- **只提交改动的任务**。`write_todos` 是整表替换，漏掉的任务会直接消失。
- **以为子 Agent 继承 Todo**。默认不继承，需要在子 Agent 的 `middleware` 中单独启用。
- **以为同名中间件会字段合并**。v0.7 是完整实例替换，替换后要重新验证权限、Backend 和子 Agent 行为。
- **把 Checkpointer 写进 `middleware=[...]`**。它是 LangGraph 运行时机制，通过 `checkpointer=` 配置。
- **手动实例化摘要中间件却不设 `trigger`**。默认 `None`，根本不会按阈值摘要。
- **看到图里有摘要节点就以为摘要已触发**。节点结构不等于运行行为。

## 6. 自测清单

- [ ] 我能写出显式启用 `write_todos` 的最小代码，并说出中间件注入的三样东西。
- [ ] 我能解释 `pending`、`in_progress`、`completed` 的含义，以及为什么 `completed` 不能替代产物验收。
- [ ] 我能说明 `write_todos` 的整表替换语义，并预测只提交部分任务后的 `todos`。
- [ ] 我能区分 `PatchToolCallsMiddleware`、`ToolRetryMiddleware`、Checkpointer 各自解决什么问题。
- [ ] 我能说出 LangChain 与 Deep Agents 两个 `SummarizationMiddleware` 在执行位置和历史保存上的差异。
