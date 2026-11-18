# 第 12 章：MCP — 用标准协议扩展 Deep Agents 工具生态

> 一句话总结：用 MCP 把外部能力做成独立 Server，再通过 `MultiServerMCPClient` 把工具转换成 LangChain Tool 传给 `create_deep_agent(tools=...)`，同时补齐命名、会话、错误与安全边界。

## 1. 这一章解决什么问题

自定义工具适合少量、稳定的应用能力；当 Agent 要连接越来越多的数据库、文件系统和外部服务时，逐个维护专用适配代码会迅速失控。MCP（Model Context Protocol）提供统一的工具与上下文描述方式，本章完成一条可分层验证的接入链路：

1. 写一个提供 `add` 与 `multiply` 的本地 MCP Server；
2. 不用模型密钥，验证工具发现、Schema 转换与真实调用；
3. 把 MCP 工具传给 `create_deep_agent(tools=...)`；
4. 理解多 Server、HTTP、会话、错误与结构化结果；
5. 补上命名、审批、子 Agent 与进程安全边界。

必须先建立一个认识：`create_deep_agent()` **不直接接收 MCP 配置，也不管理 MCP Session 生命周期**。应用先用客户端加载工具，再把 LangChain Tool 列表传入。

## 2. 核心概念与机制

### 2.1 分层位置

| 层次 | 组件 | 负责什么 |
|---|---|---|
| Agent Harness | Deep Agents | 文件系统、上下文管理、子 Agent、可选规划与工具循环 |
| Agent 工具接口 | LangChain Tool | 让模型看到名称、描述、参数 Schema 并执行调用 |
| 协议适配 | `langchain-mcp-adapters` | 把 MCP 工具转换成 LangChain Tool |
| MCP Client | `MultiServerMCPClient` | 连接一个或多个 Server，管理发现与调用会话 |
| MCP Server | 本地进程或远程服务 | 真正执行数据库、API、文件或业务操作 |

```text
用户请求 -> Agent 决定调用 -> LangChain Tool 校验参数
  -> MCP Client 建立 Session -> MCP Server 执行业务
  -> 结果转换成 ToolMessage -> Agent 继续推理
```

任务规划在 v0.7 中要显式启用 `TodoListMiddleware`；MCP Server 也不会自动获得 Deep Agents 的 State、Store 或 Backend，它仍是边界外的独立进程或服务。

### 2.2 Tools、Resources、Prompts 不是一回事

| MCP 能力 | 转换结果 | 直接传给 Agent | 用途 |
|---|---|---|---|
| Tools | `BaseTool` / `StructuredTool` | 是 | 查询数据库、调用 API、执行业务操作 |
| Resources | `Blob` | 否 | 读文件、记录或二进制资源，由应用决定如何注入 |
| Prompts | 消息列表 | 否 | 复用提示模板，由应用决定放入哪段对话 |

调用 `get_tools()` 不会让 Resources 和 Prompts 自动进入模型上下文。0.3.x 还有两个限制：Prompt 转换只接受文本，非文本 Prompt Message 抛 `ValueError`；`get_resources(..., uris=None)` 只枚举静态 Resources，不展开需要参数的 Resource Templates。

### 2.3 版本与接口边界

本章基线为 Python 3.11+、`deepagents>=0.7,<0.8`、`langchain-mcp-adapters>=0.3,<0.4`、`mcp>=1.28,<2`，按 0.3.0 源码核对 `MultiServerMCPClient` 路线。官方另有 `langchain[mcp]>=1.4.0` 的 Beta `MCPAdapter`，会话与工具生命周期不同，**不要混用两套示例**。

### 2.4 工具契约来自 Python 定义

`@mcp.tool()` 把普通函数转成 MCP Tool，三个信息直接决定模型怎么用：函数名 → `name`（模型调用的工具名）；docstring → `description`（何时调用）；类型标注 → `inputSchema`（校验参数）。

`stdio` Server 不需要监听端口：Client 把 `math_server.py` 启动为子进程，用标准输入/输出交换 MCP 消息。**不要用 `print()` 往 stdout 写调试日志**，否则日志会混入协议数据；日志应写标准错误或走 MCP 日志通知。

### 2.5 为什么必须异步、为什么结果不是整数

当前适配器把 MCP Tool 转成只有 `coroutine`、没有同步 `func` 的 `StructuredTool`。执行 `add_tool.invoke(...)` 会抛：

```text
NotImplementedError: StructuredTool does not support sync invocation.
```

因此工具发现与运行都走异步：`tools = await client.get_tools()`、`await tool.ainvoke(...)`、`await agent.ainvoke(...)`。`create_deep_agent()` 本身仍是同步的图构造函数。

`add()` 在 Server 返回 `95`，但 `ainvoke()` 返回 LangChain 标准内容块列表而非裸整数：

```python
[{"type": "text", "text": "95", "id": "..."}]
```

内容块可同时承载文本、图片和文件；示例用 `first_text()` 提取文本，避免依赖运行时生成的 `id`。

### 2.6 多 Server、传输与命名

`MultiServerMCPClient` 的连接字典可同时声明本地与远程 Server；`get_tools()` 默认并发加载全部，也可用 `server_name=` 只加载一个。

| 传输 | 配置 | 生命周期 | 场景 |
|---|---|---|---|
| `stdio` | `command`、`args`、可选 `env` | Client 启动本地子进程 | 本地开发、可信命令、桌面应用 |
| `http` | `url`、可选 `headers` / `auth` | 连接已运行的 Streamable HTTP 服务 | 远程服务、团队共享、生产 |

新项目远程连接优先 Streamable HTTP；旧式 `sse` 已被规范弃用，WebSocket 虽被实现但不是推荐主路径。静态 Bearer Token 放 `headers`，复杂 OAuth 用 MCP SDK 兼容的 `httpx.Auth`；不要让模型把 Token 当工具参数填写。

适配器默认保留原始工具名且不去重冲突。启用 `tool_name_prefix=True` 后名字变为 `<server_name>_<tool_name>`，例如 `course_math_add`。本章从第一个示例就启用，是为了新增 Server 时不必重命名已有审批规则。

### 2.7 无状态调用 vs 持久会话

默认模式无状态：`get_tools()` 用临时 Session 完成发现，之后**每次调用都新建 Session**。对 `stdio` 意味着：

```text
启动子进程 -> 初始化 MCP Session -> 调用工具 -> 关闭 Session -> 结束子进程
```

隔离清晰，但 Server 存在进程内存里的登录状态、游标或事务不会自动继承。需要跨调用保留时用 `client.session()`：

```python
async with client.session("course_math") as session:
    tools = await load_mcp_tools(
        session,
        callbacks=client.callbacks,
        tool_interceptors=client.tool_interceptors,
        server_name="course_math",
        tool_name_prefix=client.tool_name_prefix,
        handle_tool_errors=client.handle_tool_errors,
    )
    agent = create_deep_agent(model=model, tools=tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "计算 20 + 22"}]})
```

三个易忽略点：`MultiServerMCPClient` 自身不是异步上下文管理器；直接 `load_mcp_tools(session)` 不会继承 Client 的回调、Interceptor、前缀和错误策略，必须显式传；已打开的持久 Session 不会因 Interceptor 修改 `request.headers` 而重新认证。

**Session 与 LangGraph Checkpoint 是两套生命周期**：Checkpoint 保存 Agent 状态，不会序列化运行中的子进程或网络连接。长时间暂停、进程重启和 HITL 恢复不能依赖 Session 内存；重要状态应持久化在 Server 侧，写操作尽量幂等。

### 2.8 错误语义与结构化结果

从 `langchain-mcp-adapters>=0.3.0` 起，Server 返回 `CallToolResult(isError=True)` 时，默认转换成 `status="error"` 的 `ToolMessage`，Agent 可读错误后修正参数或换工具。传输失败、Session 初始化失败、内容转换失败仍抛异常。要统一走异常路径可设 `handle_tool_errors=False`，此时 `isError=True` 以 `ToolException` 抛出。

注意别把这套语义套到搜索完整性：v0.7 的 `grep`、`glob` 可成功返回部分结果并标 `truncated=True`；`ToolMessage` 没报错只证明调用成功，不证明结果完整。

MCP 的结构化部分放在 `ToolMessage.artifact["structured_content"]`，默认不会追加到模型看到的文本；如模型也需要，可在 Client 侧 Interceptor 有选择地转换，但不要无条件把大 JSON 塞进对话历史。

### 2.9 Interceptor 不是 Agent Middleware

Interceptor 包装的是“调用 MCP Tool”这一步，适合注入用户/租户信息、动态加 Header、审计、调用前拒绝、转换返回结果。`request.name` 是 Server 暴露的**原始工具名**，Server 身份看 `request.server_name`；而 `interrupt_on` 用的是最终、带前缀的 LangChain Tool 名。Server 不能直接读 LangGraph State/Store，Interceptor 只是 Client 侧桥梁，真正的授权与参数校验仍应在 Server 侧执行。

## 3. 代码与配置要点

本地 Server（`math_server.py`）：

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Chapter 12 Math")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers exactly."""
    return a + b

@mcp.tool()
def multiply(a: int, b: int) -> int:
    """Multiply two integers exactly."""
    return a * b

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

无模型冒烟测试的核心（`check_mcp.py`）：

```python
client = MultiServerMCPClient(
    {
        "course_math": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(SERVER)],
        }
    },
    tool_name_prefix=True,
)
tools = await client.get_tools()
add_tool = next(tool for tool in tools if tool.name == "course_math_add")
result = await add_tool.ainvoke({"a": 37, "b": 58})
```

交给 Agent 时只需 `tools=`：

```python
agent = create_deep_agent(
    model=model,
    tools=tools,
    system_prompt=("你是一个严谨的计算助手。数学运算必须使用 MCP 工具完成，不要直接心算。"),
)
result = await agent.ainvoke({"messages": [{"role": "user", "content": "先计算 37 + 58，再把结果乘以 12。"}]})
```

副作用工具的审批键必须写最终名：

```python
interrupt_on={"billing_charge_card": {"allowed_decisions": ["approve", "reject"]}}
```

子 Agent 若要收缩 MCP 工具，显式传 `tools` 会整体替换继承的额外工具，但不会移除 Harness 安装的文件工具或 Backend 暴露的 `execute`；省略 `interrupt_on` 继承父映射，一旦提供则整体替换。预编译 `CompiledSubAgent` 与远程 `AsyncSubAgent` 都不继承父审批。

## 4. 实验观察（若有 notebook）

### 实践建议（本章无配套 notebook）

**本章无配套 notebook。** 正文只记录了协议层的真实验证：Server 启动、工具发现、Schema 转换和直接调用；模型调用需要读者自己的凭据，编写时未代读者调用付费或外部模型。

可按以下顺序自建复现实验，任何一步失败都先停在该层：

1. **只验证 Server 函数**：先直接调用 `add(37, 58)`，确认业务逻辑。
2. **无密钥验证协议链路**：运行 `check_mcp.py`，固定版本下的关键输出应为 `tools: ['course_math_add', 'course_math_multiply']`、`description: Add two integers exactly.`、`required: ['a', 'b']`、`37 + 58 = 95`。这同时验证依赖导入、`stdio` 子进程、初始化与发现、Schema 转换、真实调用和内容块转换。
3. **再接模型**：用 `ChatOpenAI` + 硅基流动 `base_url="https://api.siliconflow.cn/v1"`，设置 `SILICONFLOW_API_KEY`、`MODEL_NAME`。不要比对整段自然语言回答，而要在 Trace 或消息历史里确认出现两次工具调用：`course_math_add(a=37, b=58) -> 95`、`course_math_multiply(a=95, b=12) -> 1140`。
4. **最后加多 Server、认证、HITL 与子 Agent**：一次只加一个变量。

若工具不被调用，按“Server 函数 → 工具发现 → LangChain Tool 直接调用 → Agent 调用 → 多 Server/认证/HITL”逐层缩小范围，不要一开始就把模型、网络认证、持久会话和多 Agent 同时接入。

## 5. 常见误区

- **以为 `create_deep_agent` 能直接吃 MCP 配置**：它只接收已转换的 LangChain Tool，Session 由应用管理。
- **用同步 `invoke()` 调 MCP 工具**：适配器只提供 `coroutine`，必须 `ainvoke()` 并让 Agent 走异步路径。
- **`stdio` Server 里 `print()` 调试**：日志混入标准输出会破坏协议解析。
- **不启用 `tool_name_prefix`**：多 Server 或与内置工具同名时，模型和审批策略难以判断目标。
- **以为默认会保留 Server 状态**：每次调用新建 Session，状态在进程内存里就丢。
- **以为持久 Session 能替代 Checkpoint**：进程重启或长暂停后连接不保证存在。
- **把 `interrupt_on` 写成原始工具名**：必须写带 Server 前缀的最终 LangChain Tool 名。
- **以为 `FilesystemPermission` 能管住 MCP 文件工具**：它只约束内置文件工具，MCP 要用 Server ACL、进程隔离与 HITL。
- **把 `stdio` 连接当普通数据**：`command`、`args`、`env` 会以当前用户权限执行，SDK 不会弹“是否信任”确认。
- **无条件把 `structuredContent` 复制进对话**：会重新制造上下文膨胀。

## 6. 自测清单

- [ ] 能画出 Deep Agent → LangChain Tool → 适配器 → MCP Client/Session → Server 的调用链，并指出谁负责 Session 生命周期。
- [ ] 能解释 Tools、Resources、Prompts 三者的转换结果与“是否自动进入模型上下文”。
- [ ] 能写出 `stdio` 与 `http` 两种连接配置，并说明 `tool_name_prefix=True` 对审批键的影响。
- [ ] 能区分默认无状态调用与 `client.session()` 的差异，以及 Session 为何不能替代 LangGraph Checkpoint。
- [ ] 能列出至少四条 `stdio` 安全要求，并说明 `isError=True`、传输失败和 `truncated=True` 分别意味着什么。
