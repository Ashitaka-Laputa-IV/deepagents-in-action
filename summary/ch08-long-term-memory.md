# 第 8 章：长期记忆 — 让 Agent 拥有跨对话的记忆

> 一句话总结：用 `CompositeBackend` 把 `/memories/` 路由到 `StoreBackend`，再用 `namespace` 划分 Agent、用户或组织作用域，就能让新 thread 读到旧对话写入的偏好；Checkpointer 仍只负责线程内的短期状态。

## 1. 这一章解决什么问题

Checkpointer 能让同一 `thread_id` 的多轮对话记住“我叫张三”，但换一个 `thread_id`，Agent 就完全不知道你是谁。真实产品需要的恰恰是跨对话信息：用户偏好、项目背景、累积资料、从反馈中学到的改进指令。这些信息不该随对话结束而消失。

本章把记忆当作**一等公民**：Agent 以文件形式读写记忆，应用用 Backend 决定文件存在哪里。流程分三步：

1. **准备存储与文件**：配置 Backend 和 Store，预置需要加载的记忆文件。
2. **加载记忆**：`memory=` 指定已有文件路径，内容进入系统提示词；`skills=` 指定包含 Skill 子目录的目录，先注入元数据，正文按需读取。
3. **更新记忆（可选）**：通过提示词约定何时写、写到哪个文件，再由 Agent 调用 `edit_file` 等工具更新。

关键限制：`memory=["/memories/preferences.md"]` 是**读取配置**。0.7.10 中缺失的记忆文件会被跳过，不会自动创建，缺失路径也不会作为已加载记忆注入提示词。要固定写入位置，必须同时约定写入路径；仅声明 `memory=` 不能保证 Agent 使用这个文件名。

## 2. 核心概念与机制

### 2.1 短期记忆与长期记忆

| | 短期记忆 | 长期记忆 |
|---|---|---|
| 机制 | Checkpointer + `StateBackend` | `StoreBackend` + LangGraph Store |
| 范围 | 单个 `thread_id` | 跨线程，按 namespace 共享 |
| 内容 | 消息历史、State 中的虚拟文件、Todo | 偏好、知识库、组织策略 |
| 生命周期 | 取决于 Checkpointer | 取决于 Store；内存 Store 重启即丢 |

`StateBackend` 把文件存在 LangGraph 的 Agent State 中。同一线程内通过 Checkpointer 持久化，多轮不丢；但**按 `thread_id` 隔离**，新线程读不到旧线程的 State 文件。只要 Checkpointer 还保存旧线程状态，回到旧线程仍能读到。

### 2.2 Checkpointer 的工作方式

每次 Agent 执行完一步，Checkpointer 自动保存当前状态（消息历史、文件系统状态、启用 Todo 后的任务清单等）；下次用相同 `thread_id` 调用时自动恢复。开发用 `MemorySaver`（内存，重启丢失），生产用 `PostgresSaver`（数据库持久化）。

对话变长后，消息可能超出上下文窗口。LangChain 有三种策略：**Trim**（只保留最近 N 条）、**Delete**（用 `RemoveMessage` 精确删除）、**Summarize**（用 LLM 压缩旧消息）。Deep Agents 已内置 Summarize（`SummarizationMiddleware`）：已知模型窗口大小时默认到 **85%** 触发，缺少窗口信息时用固定 token 阈值。正常摘要压缩的是发给模型的消息，State 中的原始消息仍保留。

v0.7 允许通过同名 `SummarizationMiddleware` 原位置换默认实例；这是整实例替换，不会只覆盖 `trigger` 或摘要模型，自定义时要把 Backend、保留消息数、触发条件和摘要提示词作为一组完整配置重新检查。长期记忆文件的更新也要区分：`write_file` 会完整覆盖，追加或局部修订应使用 `edit_file`。

### 2.3 CompositeBackend：同一套文件工具，两种存储

```python
backend=CompositeBackend(
    default=StateBackend(),
    routes={
        "/memories/": StoreBackend(
            namespace=lambda rt: (*user_namespace(rt), "memories"),
        ),
        "/skills/": StoreBackend(
            namespace=lambda rt: (*user_namespace(rt), "skills"),
        ),
    },
)
```

Agent 仍然调用同样的 `write_file`、`read_file`，区别只在路径前缀：`/workspace/draft.txt` 走 State，`/memories/preferences.md` 走 Store。**同一套工具、按路径路由**，这是设计上的核心简化。

### 2.4 三种作用域

| 作用域 | namespace | 谁能看 | 典型内容 |
|---|---|---|---|
| Agent 级 | `(assistant_id,)` | 该 Agent 的所有用户 | 自我改进的 `AGENTS.md` |
| 用户级 | `(user_id,)` | 单个用户 | `preferences.md` |
| 组织级 | `(org_id,)` | 全组织 | `compliance.md`，通常只读 |

`namespace` 函数接收 `Runtime`。部署到 LangSmith / LangGraph Server 时，`rt.server_info` 可提供 `assistant_id` 与登录用户信息；本地 `agent.invoke()` 或自托管时可能为空，所以要兜底：

```python
def user_namespace(rt):
    if rt.server_info and rt.server_info.user:
        return (rt.server_info.user.identity,)
    user_id = getattr(rt.context, "user_id", "local-user")
    return (user_id,)
```

本地调试用 `context_schema=MemoryContext` 声明上下文，调用时传 `context=MemoryContext(user_id="user-123", org_id="org-acme")`。生产环境身份必须由可信认证层提供，不能信任用户随意提交的 `user_id` / `org_id`，也不应把缺失身份统一落到默认 namespace。同一部署运行多个 Agent 时，namespace 中加 `assistant_id` 可避免记忆互相干扰；文件路径也应带主题，如 `/memories/project/tech-stack.md`。

### 2.5 记忆的六个维度

正文把记忆系统拆成可独立配置的六个维度：

| 维度 | 选项 |
|---|---|
| 持续时间 | 短期（单次对话）/ 长期（跨对话） |
| 信息类型 | 情景记忆（经历）/ 程序性记忆（Skills）/ 语义记忆（事实） |
| 作用域 | 用户级 / Agent 级 / 组织级 |
| 更新策略 | 对话中写入 / 对话间后台整合 |
| 检索方式 | 启动加载（`memory=`）/ 按需读取（Skills） |
| 权限控制 | 读写（默认）/ 只读（共享策略） |

### 2.6 高级模式

- **情景记忆**：Checkpointer 保存的线程检查点可作为过去经历的来源。`threads.search()` 只能按元数据、状态等条件筛选线程，**不接受文本查询**，所以要在客户端按关键词匹配消息正文。正文示例只搜索至多 5 个线程、每条以关键词为中心截取 300 字符、最多返回 10 条，不是完整历史搜索。
- **后台整合**：部署独立的“整合 Agent”，审查最近对话、提取事实、合并进记忆。热路径写入即时可用但增加延迟；后台整合无用户感知延迟，但下次对话才可用，且需要额外 Agent 与调度。`crons.create(assistant_id="consolidation_agent", schedule="0 */6 * * *", ...)` 可定时触发。`memory=` 本身**不会自动安排写入**。
- **并发写入**：多个线程并行写同一文件可能 last-write-wins。应按主题拆文件、用追加日志再后台合并，或让共享记忆只由后台任务写入；敏感路径加 HITL 审批。

### 2.7 从开发到生产

| 场景 | Store | 说明 |
|---|---|---|
| 本地开发 | `InMemoryStore` | 零配置，重启丢失 |
| 生产 | `PostgresStore` | 写入 PostgreSQL，真正持久化 |
| LangSmith 部署 | 平台自动提供 | 省略 `store=store`，不要自建 |

## 3. 代码与配置要点

### 3.1 跨对话访问的最小闭环

```python
store = InMemoryStore()
user = UserContext(user_id="user-123")
namespace = (user.user_id, "memories")
memory_path = "/memories/preferences.md"

# 先创建文件；CompositeBackend 会去掉 /memories/ 路由前缀。
store.put(namespace, "/preferences.md", create_file_data("# 用户偏好\n暂无记录。"))

agent = create_deep_agent(
    model=model,
    context_schema=UserContext,
    store=store,
    checkpointer=InMemorySaver(),
    backend=CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(
                namespace=lambda rt: (rt.context.user_id, "memories"),
            ),
        },
    ),
    memory=[memory_path],
    system_prompt=(
        f"当用户明确要求记住偏好时，先读取 {memory_path}，再用 edit_file 更新它。"
        "逐字保存用户给出的偏好条目，保留其他已有偏好，不要另建偏好文件。"
        "只有工具写入成功后，才告知用户已记住。"
    ),
)
```

对话 1 保存偏好，对话 2 用**新的 `thread_id`** 验证读取。验证要分开看两件事：第一轮从 Store 检查偏好是否写入；第二轮在模型调用 trace 中检查系统提示词是否包含偏好文件及已保存内容，再观察回答是否遵循。

### 3.2 预填外部数据

```python
store.put(
    memory_namespace,
    "/AGENTS.md",  # Agent 可见路径是 /memories/AGENTS.md
    create_file_data("""## Response style
- Keep responses concise
- Use code examples where possible
"""),
)
```

Store 中的文件格式（`deepagents>=0.5` 默认 v2）是：

```python
{
    "content": "第一行\n第二行\n第三行",
    "encoding": "utf-8",
    "created_at": "2024-01-15T10:30:00Z",
    "modified_at": "2024-01-15T11:45:00Z",
}
```

不要手写底层 JSON，外部预填应使用 `create_file_data`。Store key 取决于是否经过 `CompositeBackend`：

| Backend 配置 | Agent 可见路径 | Store key |
|---|---|---|
| 直接使用 `StoreBackend` | `/skills/langgraph-docs/SKILL.md` | `/skills/langgraph-docs/SKILL.md` |
| 挂载到 `routes={"/skills/": StoreBackend(...)}` | `/skills/langgraph-docs/SKILL.md` | `/langgraph-docs/SKILL.md` |

写错会得到 `/skills/skills/...`。

### 3.3 生产 Store

```python
from langgraph.store.postgres import PostgresStore
import os

with PostgresStore.from_conn_string(os.environ["DATABASE_URL"]) as store:
    store.setup()  # 第一次连接该数据库时创建表结构

    agent = create_deep_agent(
        model="google_genai:gemini-3.5-flash",
        context_schema=MemoryContext,
        memory=["/memories/AGENTS.md"],
        store=store,
        backend=CompositeBackend(
            default=StateBackend(),
            routes={
                "/memories/": StoreBackend(
                    namespace=assistant_namespace,
                ),
            },
        ),
    )
```

Web 服务或常驻进程通常在启动生命周期中初始化 Store，关闭时统一释放连接，而不是长期包在 `with` 里。

### 3.4 组织级只读记忆

```python
memory=[
    "/memories/preferences.md",
    "/policies/compliance.md",
],
permissions=[
    FilesystemPermission(
        operations=["write"],
        paths=["/policies/**"],
        mode="deny",
    ),
],
```

组织策略应由应用代码写入，Agent 只读，防止一个用户写入另一个用户会读到的指令。

## 4. 实验观察

Notebook 把“共享偏好”和“线程内草稿”放进同一个 Agent，用 Alice、Bob 两个用户验证边界。所有数据在内存中：`InMemoryStore` + `InMemorySaver` + `StateBackend`，执行版本 `deepagents==0.7.22`。

### 4.1 保存：偏好进 Store，草稿进 State

Alice 的偏好文件初始为 `{}`。保存场景依次调用 `read_file` → `edit_file` → `write_file`，实际返回：

```text
read_file(/memories/preferences.json) → success
@@ lines 1-1 of 1 @@
{}

edit_file(/memories/preferences.json) → success
Successfully replaced 1 instance(s) of the string in
  '/memories/preferences.json'

write_file(/workspace/draft.txt) → success
Updated file /workspace/draft.txt
```

随后直接读 Store，Alice 的偏好为 `{"comment_language": "zh", "variable_language": "en"}`；旧线程 State 的 `files` 只有 `['/workspace/draft.txt']`。这证明两条路径确实路由到了不同后端。

### 4.2 新线程：读到偏好，读不到草稿

换 `thread_id` 但复用同一 Store 和 `alice`：

```text
read_file(/memories/preferences.json) → success
{"comment_language": "zh", "variable_language": "en"}
read_file(/workspace/draft.txt) → error
Error: File '/workspace/draft.txt' not found
```

首次模型请求的系统提示 `<agent_memory>` 块中，偏好对象严格等于更新后的 JSON。草稿读取失败是**预期边界**，只说明新线程隔离，不代表旧线程数据被删除。

### 4.3 回到旧线程：草稿仍在，但提示词是旧的

复用 `alice-original` 的 `thread_id`，`read_file(/workspace/draft.txt)` 成功返回 `仅本线程可见：草稿 42`，checkpoint 的 `files` 里仍有草稿。但该线程恢复的是**先前已加载的记忆提示词**，即使 Store 已更新，首次请求中的 memory 仍是旧对象 `{}`。因此不能用“同一线程继续对话”证明记忆重新加载；要验证更新后的注入，必须用新线程。

### 4.4 另一个用户：namespace 隔离

Bob 使用同一个 Agent 和 Store，但 `context.user_id="bob"`。Store、真实 `read_file` 正文、首次系统 memory 三处都严格等于 Bob 的对象：

```text
{"comment_language": "en", "variable_language": "zh"}
```

Notebook 的 `validate_preferences` 同时核对 Store 记录、`read_file` 返回和 `<agent_memory>` 正文，三者任一串入 Alice 的数据都会失败。这比只看最终回复强得多。

坑与边界：

- `edit_file` 的 `old_string`、`new_string` 必须是**字符串**；`old_string` 用 `read_file` 返回的原文，`new_string` 用 `json.dumps(对象, ensure_ascii=False)`。传 JSON 对象会被真实参数校验拒绝，工具不会替你转换。
- Notebook 加了 `StopRepeatedToolErrors(stop_after=2)`：同一工具以完全相同参数反复失败达上限时，在下一次模型请求前 `jump_to="end"`，先展示实际请求和返回再判失败，避免重试到限流或超时。
- `read_file` 首行是 `@@ lines 1-N of N @@` 范围头，解析正文时要先剥离，且拒绝不完整分页。
- `memory=` 不会为新用户创建文件；未预置时不会注入任何记忆。
- `InMemoryStore` / `InMemorySaver` 只活在当前进程。实验证明的是**跨线程共享**，不是重启后的持久化。
- 实验没有测试访问控制；`user_id` 来自运行上下文，真实服务必须由认证入口提供。
- offline 脚本模型按固定调用表行动，不证明真实模型会自主选择这些工具；本章 live 截至 2026-10-06 尚无整本通过记录，复测中真实模型曾在保存场景反复提交同一组错误的 `edit_file` 参数。

## 5. 常见误区

- **把 `memory=` 当成写入配置**：它只加载已有文件；写入要在提示词中约定路径并由 Agent 调工具。
- **以为缺失的记忆文件会自动创建**：会被跳过，也不会出现在提示词里。
- **在同一线程验证“重新加载”**：0.7.10 会复用 state 中已有的 `memory_contents`；必须用新 `thread_id`。
- **把 Checkpointer 当长期记忆**：它按 `thread_id` 隔离，跨线程读不到。
- **Store key 多写挂载前缀**：`/preferences.md` 与 `/memories/preferences.md` 是两回事。
- **用 `write_file` 做局部更新**：它会完整覆盖，追加或修订应用 `edit_file`。
- **以为 `memory=` 会自动安排后台整理**：不会；要么热路径写入，要么显式部署整合 Agent 和 Cron。
- **默认用户级 namespace 就安全**：身份不可信时仍会串数据；组织级记忆应只读。
- **忽略并发写**：同一文件 last-write-wins，应按主题拆文件或串行化写入。
- **以为 `InMemoryStore` 能跨进程**：进程退出即丢，生产要 `PostgresStore` 或平台 Store。

## 6. 自测清单

- [ ] 能区分 Checkpointer、`StateBackend`、`StoreBackend` 三者的职责与可见范围。
- [ ] 能写出 `CompositeBackend` + `memory=[...]` 的完整配置，并说明 `/memories/` 前缀在 Store key 中为何要剥掉。
- [ ] 能为 Agent 级、用户级、组织级记忆各写一个 `namespace` 函数，并说明身份应从何处获取。
- [ ] 能解释 `memory=` 与 `skills=` 的参数类型差异，以及为什么缺失文件不会被加载或创建。
- [ ] 能设计一个验证流程，分别核对 Store 内容、`read_file` 结果和新线程首次模型请求中的 `<agent_memory>`。
