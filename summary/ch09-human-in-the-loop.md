# 第 9 章：Human-in-the-Loop — 构建安全的人机协作流程

> 一句话总结：`interrupt_on` 让指定工具在真正执行前暂停，人类用 `approve` / `edit` / `reject` / `respond` 做出决策，再通过同一 `thread_id` 的 `Command(resume=...)` 恢复；底层是 LangGraph `interrupt()` 加 Checkpointer 的状态存档与节点重放。

## 1. 这一章解决什么问题

Agent 越自主，越可能删错文件、发错邮件、调用昂贵 API 或改坏生产配置；完全自主不可接受，完全人工又失去价值。需要一条中间地带：Agent 照常规划，但在敏感动作真正执行前**暂停**，让人检查参数、修改、拒绝，或直接替工具回答。

本章先讲 `interrupt_on`，再解释底层 `interrupt()`：只有理解暂停与重放，才能避免重复副作用。

## 2. 核心概念与机制

### 2.1 `interrupt_on` 与中间件栈

设置 `interrupt_on` 后，Deep Agents 在默认中间件栈中加入 `HumanInTheLoopMiddleware`。如果运行在工具返回前被中断或取消，同一栈里的 `PatchToolCallsMiddleware` 会自动修复消息历史。

`interrupt_on` 是“工具调用审批”的便捷入口，**不是 HITL 的能力边界**。若暂停点不对应某个工具——发布最终答复前审稿、进入下一阶段前确认、跨多个工具统一策略——可以在自定义 LangChain Middleware 中直接调用底层 `interrupt()`，再通过 Deep Agents 的 `middleware=[...]` 注入。

### 2.2 三种配置值与四种决策

| 配置值 | 含义 |
|---|---|
| `True` | 启用中断，允许所有决策（approve / edit / reject / respond） |
| `False` | 不中断，直接执行 |
| `{"allowed_decisions": [...]}` | 启用中断，只允许指定类型 |

| 决策 | 含义 | 典型场景 |
|---|---|---|
| `approve` | 批准，按 Agent 原始参数执行 | 确认删除这个文件 |
| `edit` | 修改参数后执行 | 改收件人再发 |
| `reject` | 跳过调用，把拒绝原因反馈给 Agent | 不要删除，取消 |
| `respond` | 不执行工具，把人的 `message` 当作一次成功的合成工具结果 | `ask_user` 这类询问工具 |

最容易混淆的是 `reject` 与 `respond`：**拒绝副作用工具必须用 `reject`**，否则 `respond` 会被模型当成成功的 ToolMessage，等于告诉它“工具已经做完了”。`respond` 只适合询问型工具；`edit` 应只做保守修改，大幅改写参数可能让模型重估计划并重复调用。

### 2.3 条件中断

默认只要工具名在 `interrupt_on` 里，每次调用都暂停。可以在配置中加 `when` 谓词：

```python
def writes_outside_workspace(request: ToolCallRequest) -> bool:
    """只有写入工作区外的路径时才暂停。"""
    path = request.tool_call["args"].get("file_path", "")
    if not isinstance(path, str) or not path.startswith("/"):
        return True
    normalized = posixpath.normpath(path)
    return not normalized.startswith("/workspace/")
```

返回 `True` 中断，`False` 放行。条件中断需要 `langchain>=1.3.3`。示例先规范化 POSIX 路径，避免 `/workspace/../secrets/token` 被前缀检查放行；但这仍不是宿主符号链接或竞态防护，真实边界要靠 Backend、权限与隔离环境。

### 2.4 中断与恢复的完整流程

1. Agent 正常运行，直到调用配置了 `interrupt_on` 的工具。
2. 执行暂停，返回中断信息。
3. 用户检查 `action_requests` 与 `review_configs`，做出决策。
4. 用**相同的 `thread_id`** 和 `Command(resume={"decisions": [...]})` 恢复。

关键要求：

- 必须配置 Checkpointer；否则无法保存和恢复状态。
- 必须使用相同 `thread_id`；换线程找不到存档。
- `decisions` 的数量和顺序必须与 `action_requests` 一一对应。
- 本章用 `version="v2"` 读取 `result.interrupts` 和 `result.value`；默认 `invoke()` 也支持，中断在 `__interrupt__` 中，v2 不是必要条件。

审批发生在独立的 `HumanInTheLoopMiddleware.after_model` 节点：工具请求已进入 State，恢复时复用它再执行。字段名有差异：Deep Agents 常见 `action_request["args"]`，LangChain 标准示例展示 `action_request["arguments"]`；读取可兼容两者，但 `edit` 的 `edited_action` 仍用 `args`。

### 2.5 底层 `interrupt()` 到底做了什么

`interrupt()` 不是普通 `input()`：

1. 节点调用 `interrupt(value)`。
2. 运行时把 `value` 包装成 `Interrupt` 对象，包含审查数据和稳定 `id`。
3. Checkpointer 保存当前线程状态、下一步节点、待恢复中断。
4. 执行暂停，调用方拿到 `result.interrupts` 或 `stream.interrupts`。
5. 人类返回 `Command(resume=...)`。
6. 运行时按同一 `thread_id` 找回 checkpoint，把恢复值送回对应的 `interrupt()` 调用。

关键在恢复方式：LangGraph **从节点开头重新执行**，而不是从 `interrupt()` 的下一行继续。`interrupt()` 会用已有恢复值直接返回，不再暂停。因此 `interrupt()` 之前的代码会再跑一次。

### 2.6 五条使用规则

1. **不要用裸 `try/except` 包裹 `interrupt()`**：它通过特殊异常暂停，裸 `except Exception` 会吞掉它；只捕获具体异常。
2. **`interrupt()` 之前的副作用必须幂等**：恢复会重跑前置代码。用 upsert，或把副作用放到 `interrupt()` 之后。
3. **不要动态改变 `interrupt()` 的调用顺序**：同一节点内多个中断按索引匹配，条件跳过会导致错位。
4. **并行中断用 `Interrupt.id` 映射恢复**：不要依赖界面排序。
5. **`resume` 后节点从头重放**：把动作放在审批之后只能避免“暂停前提前执行”，**不提供 exactly-once 保证**；后续中断、失败重试或进程恢复仍可能重放。不能重复的动作要用幂等键、事务或独立任务。

多轮输入验证不要用 `while True` 反复 `interrupt()`，因为每轮恢复都会重跑前几轮；应每次只暂停一次，把下一轮问题写回状态，用条件边回到同一节点。

### 2.7 何时直接用底层 `interrupt()`

| 场景 | 推荐方式 |
|---|---|
| 给某个工具加审批 | `interrupt_on` |
| 按工具参数决定是否审批 | `interrupt_on` + `when` |
| 模型调用前后统一审查 | Node-style Middleware + `interrupt()` |
| 节点中收集缺失信息 | 直接 `interrupt()` |
| 多轮表单 / 输入验证 | 单次 `interrupt()` + 条件边 |
| 调试图执行路径 | `interrupt_before` / `interrupt_after` |
| 并行人工任务汇总 | 底层 `interrupt()` + ID 映射 |

`before_model`、`after_model` 等 Node-style Hook 是推荐的中断位置，因为它们是图中的独立节点。Wrap-style Hook 也能调用 `interrupt()`，但运行在 model/tools 节点内部，`handler` 可能被跳过或执行多次；在 `handler()` 后中断，恢复会重放整个节点。它更适合重试、缓存和转换。

### 2.8 批量、子 Agent 与文件系统权限

- **批量调用**：同一条模型消息的多个待审批工具打包成**一个**中断，`decisions[0]` 对应 `action_requests[0]`，按位置配对。
- **子 Agent**：可以有独立的 `interrupt_on`，覆盖主 Agent 的设置。主 Agent 读文件不需要审批，子 Agent 读文件也可以要求审批。
- **文件系统权限中断**：`FilesystemPermission(..., mode="interrupt")` 会让内置 `write_file`、`edit_file`、`delete` 命中规则时抛出同格式中断，并与 `interrupt_on` 合并。`operations=["write"]` 也覆盖覆盖写入和删除；含受保护后代的目录删除会整体拒绝。需要 `deepagents>=0.6.8`。

按风险分层：高风险（删除、发送、部署、SQL）用 `approve/edit/reject`；中风险（写入、外部调用）用 `approve/reject`；只读设 `False`；询问工具只用 `respond`。

## 3. 代码与配置要点

### 3.1 基本配置

```python
checkpointer = MemorySaver()

agent = create_deep_agent(
    model=model,
    tools=[delete_file, read_demo_file, send_email],
    interrupt_on={
        "delete_file": {"allowed_decisions": ["approve", "edit", "reject"]},
        "read_demo_file": False,
        "send_email": {"allowed_decisions": ["approve", "reject"]},
    },
    checkpointer=checkpointer,
)
```

### 3.2 暂停、检查、恢复

```python
config = {"configurable": {"thread_id": str(uuid.uuid4())}}

result = agent.invoke(
    {"messages": [{"role": "user", "content": "删除 temp.txt 文件"}]},
    config=config,
    version="v2",
)

if result.interrupts:
    interrupt_value = result.interrupts[0].value
    action_requests = interrupt_value["action_requests"]
    review_configs = interrupt_value["review_configs"]

    decisions = [{"type": "approve"}]

    result = agent.invoke(
        Command(resume={"decisions": decisions}),
        config=config,
        version="v2",
    )
```

### 3.3 `edit` 与 `reject`

```python
action_request = interrupt_value["action_requests"][0]

decisions = [{
    "type": "edit",
    "edited_action": {
        "name": action_request["name"],
        "args": {
            "to": "team@example.com",
            "subject": "通知",
            "body": "...",
        },
    },
}]

decisions = [{
    "type": "reject",
    "message": "用户拒绝删除该文件。不要再次尝试删除，请询问是否改为归档文件。",
}]
```

`edited_action` 必须同时提供工具名和完整 `args`。`reject` 不传 `message` 时，默认反馈会告诉模型工具没有执行、不要重复调用；敏感工具建议写清下一步。

### 3.4 `respond` 只用于询问工具

```python
@tool
def ask_user(question: str) -> str:
    """向用户提问；真实回答由 HITL 的 respond 决策提供。"""
    return "等待用户回答"

decisions = [{
    "type": "respond",
    "message": "使用季度维度，并排除测试数据。",
}]
```

人的回答会成为 `ask_user` 的成功返回值，占位函数不会执行。

### 3.5 文件系统权限中断

```python
agent = create_deep_agent(
    model=model,
    permissions=[
        FilesystemPermission(
            operations=["write"],
            paths=["/secrets/**"],
            mode="interrupt",
        ),
    ],
    checkpointer=MemorySaver(),
)
```

恢复方式与普通工具一致，检查 `result.interrupts[0].value["action_requests"]`，再提交 `Command(resume=...)`。

## 4. 实验观察

Notebook 用只写内存列表的教学 `send_email` 和占位 `ask_user` 观察审批，**不发送真实邮件**；正文按 `deepagents==0.7.15` 核对，Notebook 执行 `deepagents==0.7.22`、`langgraph==1.2.13`，offline 脚本模型只按计划提出调用，中间件、工具和恢复流程真实执行。

### 4.1 暂停时工具还没有执行

发送上线通知后，Notebook 检查到：

```text
待审批动作 1：send_email
  调用 ID： mail-1
  参数：{"to": "all@example.com", "subject": "上线通知", "body": "..."}
  允许决策： approve, edit, reject
工具执行次数： 0
```

`get_state` 显示：

```text
待执行节点： HumanInTheLoopMiddleware.after_model
待解决中断数： 1
State 已保存的调用： send_email
审批前实际模型请求数： 1
```

`AIMessage.tool_calls` 只是请求；审批前工具没跑，但请求已存进 State，恢复时可复用。

### 4.2 四种决策的真实效果

| 决策 | 执行记录 | 工具结果 | 观察 |
|---|---|---|---|
| `approve` | `send_email` 执行 1 次，参数为原始 `all@example.com` | `status="success"` | 模型请求从 1 变 2，第二次读取工具结果；整次完成 |
| `edit` | 执行 1 次，收件人变为 `team@example.com` | `status="success"`，附人类替换说明和新参数 | 原请求仍留在历史中，实际执行用修改后的参数 |
| `reject` | 0 次 | `status="error"`，内容含 `User rejected`、工具名和完整拒绝原因 | 拒绝原因进入恢复后的模型输入 |
| `respond` | 0 次 | `status="success"`，内容就是人的回答 | 占位 `ask_user` 没运行，但模型收到了成功结果 |

`edit` 的工具返回会提示“人类审阅者在运行前替换了这次调用，不要重新发起原调用”，并附实际参数，减少重复调用。

### 4.3 批量与数量校验

同一条消息提出两个 `send_email`（`team@example.com` 与 `all@example.com`）。批准第一个、拒绝第二个后，执行记录只有 1 条；第二个得到 `status="error"` 和拒绝原因。`decisions` 按位置配对，不按工具名，同名工具写反也不会报错，必须保留请求顺序。

只提交一个决策会得到真实报错：

```text
ValueError: Number of human decisions (1) does not match number of hanging tool calls (2).
```

工具执行次数仍为 **0**，说明校验失败发生在执行前。

### 4.4 第一次回答生效不等于整次运行完成

两轮问答场景中，第一次 `respond` 后：

```text
工具结果： ask_user 状态： success 调用 ID： question-1
待执行节点 snapshot.next： ('HumanInTheLoopMiddleware.after_model',)
中断数（返回 / 快照）： 1 / 1
待审批动作： ask_user {'question': '报告输出 PDF 还是表格？'}
整次运行： 等待下一次人工审批
```

首问回答生效，但第二次模型请求又提出新的 `ask_user`，产生新中断；再提交第二回答后整次才完成，最终主请求数为 3。“本批决策”和“整次完成”是两个判断。

坑与边界：

- **`reject` 的 `error` 不是 Python 异常**：它表示调用被拒绝，工具函数没有运行。
- **offline 脚本模型不证明真实模型会选对工具**；随附输出仍为 offline。live 记录为 `Qwen/Qwen3-Coder-30B-A3B-Instruct` 于 2026-10-06 整本通过。
- **`InMemorySaver` 只活在当前内核**：跨进程恢复、数据库持久化、并发审批、真实邮件投递都未验证。
- **工具执行次数断言不承诺 exactly-once**：邮件已发但结果未保存就崩溃，恢复仍可能重发。
- **教学工具收窄不是安全边界**：Notebook 用 `wrap_model_call` 收窄模型可见工具，但未移除图中其他工具，也未建立执行权限系统。
- **章节首个示例与 Notebook 配置不同**：正文首个 `send_email` 只允许 `approve`/`reject`，Notebook 的教学配置允许 `approve`/`edit`/`reject`；照抄正文示例时不能直接提交 `edit`。

## 5. 常见误区

- **把 `respond` 当“软拒绝”**：它会让模型以为工具成功执行；拒绝副作用工具要用 `reject`。
- **以为 `reject` 会先执行再回滚**：工具根本不会运行，也不存在回滚。
- **以为 `edit` 只改展示文本**：实际执行使用修改后的参数，必须核对执行记录。
- **以为恢复从 `interrupt()` 下一行继续**：节点从头重放，前置代码会再跑。
- **用裸 `try/except` 包 `interrupt()`**：会把暂停异常吞掉。
- **`interrupt()` 前做非幂等副作用**：恢复时可能重复创建记录、重复发送。
- **动态跳过 `interrupt()`**：同一节点内按索引匹配，顺序变化会导致答案错配。
- **批量决策按工具名匹配**：实际按位置，同名工具尤其危险。
- **没有 Checkpointer 或换了 `thread_id`**：无法恢复，或恢复到另一条会话。

## 6. 自测清单

- [ ] 能解释 `interrupt_on` 的三种配置值，并针对高风险、中风险、低风险、人工输入型工具各给出配置。
- [ ] 能区分 `approve`、`edit`、`reject`、`respond` 的执行效果，尤其能说明为什么副作用工具不能用 `respond`。
- [ ] 能写出一次完整的中断—检查—`Command(resume=...)` 恢复流程，并说明 Checkpointer、`thread_id`、决策顺序为何都是必要条件。
- [ ] 能解释 `interrupt()` 的异常暂停、Checkpoint 保存、节点从头重放，并列出至少三条由此产生的编码规则。
- [ ] 能处理批量审批和第二次中断，说明“本批决策成功”与“整次运行完成”的区别。
