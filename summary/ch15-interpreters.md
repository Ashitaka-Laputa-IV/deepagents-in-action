# 第 15 章：Interpreters — 让 Agent 用代码编排工具与数据

> 一句话总结：`CodeInterpreterMiddleware` 把模型生成的 JavaScript 交给进程内的 QuickJS 执行，让循环、分支和聚合留在代码里；PTC 再把白名单工具映射成 `tools.*` 异步函数，使批量任务只把整理后的结果带回对话。

## 1. 这一章解决什么问题

订单审核 Agent 收到 80 个订单号。它调用一次查询工具，等结果回来，再决定下一次调用。几十条完整结果陆续进入上下文，模型还没审完就开始漏项。查询工具没有错，错的是编排方式：循环、筛选和聚合全部由模型逐轮决定。

普通 Tool Calling 的局限可以归纳为四点：调用次数由模型决定，难以保证每个输入都被处理；每个中间结果都进入上下文，占用 token 并干扰判断；排序、分组、去重等确定性工作被反复执行；调用被拆到更多模型轮次，延迟和成本上升。

模型可以在同一轮发出一批调用，但这批调用在生成完成时就已固定，不能读取第一个结果再决定第二个；循环、分支和重试通常需要新模型轮次。

Interpreter 提供另一条路径：模型生成一段 JavaScript，由 QuickJS 在 Agent 循环内执行，模型只接收最后的结果。它适合“纯内存数据处理”和“大量外部调用需要循环/并行”两类任务；需要 Shell、安装依赖、运行测试或完整文件系统时，应使用 Sandbox。

| 任务形状 | 优先选择 | 原因 |
| --- | --- | --- |
| 一两个简单外部调用 | 普通 Tool Calling | 路径短，额外编排没有收益 |
| 纯内存排序、分组、解析或校验 | Interpreter | JavaScript 可确定性处理数据 |
| 大量外部工具调用，需要循环或并行 | Interpreter + PTC | 代码控制调用与聚合，只返回必要结果 |
| Shell、依赖安装、测试、完整文件系统 | Sandbox | 需要独立执行环境和操作系统能力 |
| 大量独立任务需要不同 Agent 角色 | Dynamic Subagents | 每个角色要运行完整子 Agent 推理循环 |

Interpreter 不是轻量版 Shell，也不是 Sandbox Backend。它是 Agent 循环内、由 WebAssembly 隔离的内存代码运行时。

## 2. 核心概念与机制

### 2.1 模型只负责“写代码”，运行时负责“执行”

`CodeInterpreterMiddleware` 向 Agent 增加一个 `eval` 工具。模型像调用其他工具一样调用 `eval`，把自己生成的 JavaScript 交给 QuickJS。`eval` 返回最后一个表达式的值；`console.log`、`console.warn`、`console.error` 默认被捕获进响应，但它们是调试手段，不应代替最终表达式。

```mermaid
flowchart LR
    U[用户请求] --> M[主 LLM]
    M -->|调用 eval| Q[QuickJS / Wasmtime]
    Q -->|tools.lookupOrder| P[PTC tool proxy]
    P --> T[Python 工具]
    T --> P
    P --> Q
    Q -->|只返回汇总对象| M
```

关键点：模型不直接操作 QuickJS API；它写普通 JavaScript，工具通过 `tools` 命名空间暴露。

### 2.2 QuickJS 默认是封闭的

模型可以写任意代码，但代码不会自动获得宿主机的文件、网络或 Shell 权限。

| 能力 | 默认是否可用 | 说明 |
| --- | --- | --- |
| JavaScript 与顶层 `await` | 是 | 循环、分支、Promise、数据转换 |
| `console.log/warn/error` | 是 | 输出进入 `eval` 响应 |
| Agent 工具 | 否 | 需显式配置 PTC 白名单 |
| 文件系统 / 网络 / 系统时间 | 否 | 需通过 PTC 暴露具体工具 |
| Shell、包管理器、系统测试 | 否 | 应使用 Sandbox |

### 2.3 Interpreter 与 Sandbox 的边界

Interpreter 在 Agent 进程内编排工具、保存中间值和转换数据，默认没有文件与网络；Sandbox 在独立容器或虚拟机中操作文件、进程和依赖，编译、测试、安装包应使用它。

### 2.4 PTC：把工具映射成 `tools.*`

Programmatic Tool Calling 不是 Provider 自带的批量工具协议。`CodeInterpreterMiddleware` 通过 PTC runtime / tool proxy，把白名单里的 Python 工具映射到 QuickJS 的 `tools.*` 命名空间。两种配置形式不能混淆：

- `ptc=[lookup_order]`：直接传 `BaseTool` 对象，工具只在解释器的 `tools` 命名空间中可用，不会同时成为 Agent 的普通工具。这能收紧调用路径，迫使模型先 `eval`。
- `ptc=["lookup_order"]`：按名称从 Agent 工具集匹配；工具本来也应该支持普通 Tool Calling 时使用。

命名转换只发生在函数名：Python 的 `lookup_order` 变成 `tools.lookupOrder`；输入对象仍遵循原 Schema，参数继续叫 `order_id`。写 `tools.lookupOrder({ orderId: ... })` 会校验失败。

PTC 改变的是调用路径：

| 对比项 | 普通 Tool Calling | PTC |
| --- | --- | --- |
| 谁控制下一次调用 | 模型下一轮输出 | 当前 `eval` 中的 JavaScript |
| 循环和分支 | 通常需要新模型轮次 | 代码内直接完成 |
| 中间结果 | 每次返回模型上下文 | 留在解释器，整理后再返回 |
| 并行批次 | 模型决定一批调用 | `Promise.all` 等代码决定 |
| 审批路径 | 走普通工具调用机制 | 不逐次走普通工具调用路径 |

最后一行是安全边界：PTC 调用不会逐次执行父 Agent 的 `interrupt_on` 审批。转账、删库、发信、创建云资源等高副作用工具，不应因为“批量方便”就放进白名单。若业务要求每次写操作都人工确认，保留普通工具路径；若允许一次批准整批流程，可以把 `eval` 作为审批边界，但同时必须限制白名单、最大调用次数和工具自身权限。

### 2.5 状态保留范围：`call`、`turn`、`thread`

| `mode` | 状态范围 | 适合场景 | 主要风险 |
| --- | --- | --- | --- |
| `"call"` | 只保留当前一次 `eval` | 无状态转换、隔离最强 | 多次 `eval` 需重新准备数据 |
| `"turn"` | 当前 Agent turn 内的多次 `eval` | 一次请求分步计算 | 下一轮不能复用结果 |
| `"thread"` | 同一线程的多个 Agent turn | 分段分析、跨轮累积 | 旧变量可能影响后续请求 |

`thread` 模式在 Agent turn 开始时恢复最近的 Interpreter Snapshot，结束时把新快照写进 Graph State；一个 turn 内的多次 `eval` 共用同一上下文。本章锁定版本中，Snapshot 保存的是 QuickJS guest 的整个 VM 内存，不止 JSON 数据：变量、函数、闭包状态和对象引用关系都能一起恢复。因此不要误以为“返回给模型的结果要可序列化”等于“快照只能存普通数据”。快照依赖兼容的 guest build，不能当作跨版本的通用存档；升级后要单独测试恢复并保护存储免受篡改。跨轮状态不需要自己实现存储，给 Agent 加 LangGraph Checkpointer 并复用同一个 `thread_id` 即可。

### 2.6 预算参数与排错顺序

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `memory_limit` | 64 MB | 限制每个线程的 QuickJS 堆内存 |
| `timeout` | 5 秒 | 每次 `eval` 的 JavaScript 执行预算；不含等待异步宿主调用的时间 |
| `max_result_chars` | `4000` | 截断结果、错误和标准输出 |
| `ptc` | `None` | PTC 白名单；省略时关闭 |
| `max_ptc_calls` | `256` | 每次 `eval` 的 PTC 调用上限 |
| `mode` | `"thread"` | 状态保留范围 |
| `max_snapshot_bytes` | `None` | 快照大小上限，默认同内存上限 |

`tool_name` 默认为 `"eval"`，`capture_console` 默认 `True`，`subagents` 默认 `True`。

`max_ptc_calls=256` 是运行时上限，不是建议用满。订单实验只有七个输入，所以正文把它收紧到 16。真实应用应按最大合法批次设置，并让工具自身继续做鉴权、配额和参数校验。

`timeout` 不是整个订单流程的端到端截止时间：等待异步宿主调用的时间不计入预算。网络工具需要自己的超时，应用也要设置整次运行的截止时间。

| 现象 | 首先检查 |
| --- | --- |
| Agent 从不调用 `eval` | 模型是否支持 Tool Calling；任务是否真需要批处理 |
| JavaScript 中没有目标工具 | `ptc` 用的是名称还是 `BaseTool` 对象；名称是否匹配 Agent 工具集 |
| 提示 `tools.lookup_order` 不存在 | 是否忘了函数名转 camelCase |
| 工具输入校验失败 | 参数是否仍按 Python Tool Schema 命名 |
| 批处理运行到一半停止 | 是否触发 `timeout` 或 `max_ptc_calls` |
| 跨轮状态丢失 | `mode`、`thread_id`、快照上限与 guest build 是否匹配 |

PTC 的中间步骤由 PTC runtime / tool proxy 执行，不要假设第 14 章的流式前端会自动把每个 `tools.*` 调用渲染成普通工具卡片。需要审计时，应同时记录 `eval` 输入、PTC 工具自身日志和最终结果摘要，并对敏感参数脱敏。

### 2.7 安全：白名单不等于完整隔离

QuickJS 默认没有文件、网络和 Shell 能力；一旦把工具接入 PTC，工具能做什么，解释器代码就能做什么。设计白名单前先问四个问题：

1. 这个任务是否真的需要该工具？
2. 工具能否读取秘密、访问任意路径或调用任意 URL？
3. 工具是否会花钱、修改数据或触发外部通知？
4. 单次 `eval` 最多允许调用多少次？

优先暴露窄工具，例如“按 ID 读取一个订单”，不要暴露“执行任意 SQL”或“向任意 URL 发请求”。工具内部仍要校验调用者、资源范围和参数；PTC 白名单不能替代业务鉴权。

本章的 `langchain-quickjs==0.3.5` 依赖 `quickjs-rs>=0.2.5,<0.3`：运行时由进程内 Wasmtime 执行编译为 WebAssembly 的 QuickJS，每个 LangGraph thread 使用独立实例。但 Wasmtime 漏洞可能突破隔离，PTC 暴露的宿主工具也保留真实权限。高风险多租户应叠加进程或容器隔离；需要 Shell 用第 10 章 Sandbox，人工确认用第 9 章 HITL，文件范围由第 11 章权限规则控制。

## 3. 代码与配置要点

### 3.1 环境

```bash
uv add "deepagents[quickjs]==0.7.8" "langchain-quickjs==0.3.5" langchain-openai
uv run python -c "import sys, deepagents, langchain_quickjs; print(sys.version_info[:2]); print(langchain_quickjs.__name__)"
```

要求 Python 3.11+；正文在 Python 3.11.14、`deepagents==0.7.8`、`langchain-quickjs==0.3.5` 中核对。Interpreters 目前是 Beta API，接口与生命周期仍可能变化。

### 3.2 纯内存 JavaScript

```python
from deepagents import create_deep_agent
from langchain_quickjs import CodeInterpreterMiddleware

agent = create_deep_agent(
    model=model,
    system_prompt="Use eval for deterministic filtering and aggregation.",
    middleware=[CodeInterpreterMiddleware(mode="call")],
)
```

模型生成的代码可能不同，核心逻辑类似：

```typescript
const risky = orders
  .filter((order) => order.amount >= 1000 || order.refunds >= 2)
  .sort((left, right) => right.amount - left.amount);

({
  ids: risky.map((order) => order.id),
  totalAmount: risky.reduce((sum, order) => sum + order.amount, 0),
});
```

返回的是最后一个表达式 `{ids, totalAmount}`，不是整个执行过程。

### 3.3 启用 PTC

```python
agent = create_deep_agent(
    model=model,
    middleware=[
        CodeInterpreterMiddleware(
            ptc=[lookup_order],
            mode="turn",
            max_ptc_calls=16,
        )
    ],
)
```

批处理代码：

```typescript
const rows = await Promise.all(
  ids.map((orderId) => tools.lookupOrder({ order_id: orderId })),
);

const missing = rows
  .filter((row) => row.error === "not_found")
  .map((row) => row.order_id);

const risky = rows
  .filter((row) => !row.error)
  .filter((row) => row.amount >= 1000 || row.refunds >= 2)
  .sort((left, right) => right.amount - left.amount);

({ riskyIds: risky.map((row) => row.order_id), totalAmount: risky.reduce((sum, row) => sum + row.amount, 0), missing });
```

七次查询仍真实发生，但模型不需要读取七份完整工具消息；QuickJS 等待结果、过滤正常记录、整理缺失项，只把小对象返回。

### 3.4 跨轮状态

```python
from langgraph.checkpoint.memory import MemorySaver

agent = create_deep_agent(
    model=model,
    middleware=[CodeInterpreterMiddleware(mode="thread")],
    checkpointer=MemorySaver(),
)

config = {"configurable": {"thread_id": "order-review-001"}}
```

后续每次 `agent.invoke(..., config=config)` 都要复用同一个 `thread_id`。`max_snapshot_bytes=4 * 1024 * 1024` 可作为限制快照大小的起点；`MemorySaver` 只适合进程内实验，服务重启后仍需恢复线程时应换持久化 Checkpointer。

## 4. 实验观察（若有 notebook）

本章无配套 notebook。以下建议来自正文的可复现步骤与验收条件。

### 实践建议

1. **先纯内存，后 PTC。** 用 `mode="call"` 和 Prompt 内订单数组确认模型调用 `eval`；再接入 `lookup_order`。
2. **固定输入验收。** 提交 `A-100` 到 `A-105` 与不存在的 `A-999`，代表性结果为风险订单 `A-104、A-101、A-102、A-105`、总金额 `5320`、未找到 `A-999`。按五条检查：调用 `eval`、`Promise.all` 覆盖全部编号、`A-999` 归入缺失、金额降序且由代码汇总、模型只收到整理后的对象。
3. **Trace 不对先查能力。** 模型没用 `eval` 时检查 Tool Calling 与系统提示；不要为让示例成功删掉输入。
4. **审计与预算。** 记录 `eval` 输入、PTC 日志和最终摘要；按合法批次收紧 `max_ptc_calls`，结果过长先在 JavaScript 内聚合。
5. **副作用补偿。** 恢复 Snapshot 不回滚数据库、网络或付款；有副作用工具要设计幂等键、补偿或事务。

## 5. 常见误区

1. **把 Interpreter 当 Sandbox。** 它没有文件、网络、Shell 和包管理能力；需要这些时应使用 Sandbox Backend。
2. **把 PTC 当 Provider 的批量工具协议。** 它是中间件通过 runtime/tool proxy 做的本地映射。
3. **把函数名和参数名一起 camelCase。** 只有函数名变 `tools.lookupOrder`，参数仍是 `order_id`。
4. **以为 PTC 会逐次走 `interrupt_on`。** 高风险写操作应留在普通工具路径，或把 `eval` 本身设为审批点。
5. **以为 Snapshot 能回滚外部副作用。** 它只保存 QuickJS VM 内存。
6. **把 `timeout` 当整条工作流的截止时间。** 异步宿主调用的等待不计入。

## 6. 自测清单

- [ ] 能根据任务形状判断该用普通 Tool Calling、Interpreter、Sandbox 还是 Dynamic Subagents。
- [ ] 能写出 `ptc=[lookup_order]` 与 `ptc=["lookup_order"]` 两种配置，并说明暴露范围差异。
- [ ] 能解释 `call`、`turn`、`thread` 三种状态范围及各自风险。
- [ ] 能为一次 `eval` 设置内存、超时、调用次数和结果长度四类预算。
- [ ] 能列出 PTC 白名单设计前的四个安全问题，并说明为什么白名单不能替代鉴权。
