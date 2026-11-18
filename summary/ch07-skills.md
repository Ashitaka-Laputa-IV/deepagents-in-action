# 第 7 章：Skills — 可复用的 Agent 能力包

> 一句话总结：Skill 是遵循 Agent Skills 规范的能力目录（`SKILL.md` 加可选的 `scripts/`、`references/`、`assets/`），通过“元数据 → 正文 → 资源”的三级渐进披露按需进入上下文，并可用不同 Backend 与权限规则控制存储和写入。

## 1. 这一章解决什么问题

工具是原子操作：搜索一次、读一个文件、调一次 API。但“按照团队规范做代码审查”“查阅 LangGraph 最新文档再回答”“生成符合公司格式的技术报告”这类能力，需要的是**多步骤工作流 + 领域知识 + 模板资源**的组合。把它们全塞进系统提示词会持续占用上下文；为每个流程新增一个工具又难以表达步骤和判断标准。

Skill 把这类能力打包成目录，并遵循开放的 Agent Skills 规范，而不是 Deep Agents 私有概念。按正文，截至 2026 年已有 30+ 个主流 AI 开发工具采用，包括 Claude Code、OpenAI Codex、Gemini CLI、Cursor、VS Code、Goose、Letta、GitHub、Databricks 等；写一个 Skill 可在所有兼容产品中复用。作者的类比是：Skills 之于 Agent，如同 npm 包之于 Node.js。

## 2. 核心概念与机制

### 2.1 目录结构

```text
skills/
└── langgraph-docs/
    ├── SKILL.md              # Required: metadata + instructions
    ├── scripts/              # Optional: executable scripts
    │   └── fetch_docs.py
    ├── references/           # Optional: detailed docs
    │   ├── api-patterns.md
    │   └── style-guide.md
    └── assets/               # Optional: templates, data files
        ├── report-template.md
        └── schema.json
```

| 目录 | 用途 | 加载时机 |
|---|---|---|
| `SKILL.md` | 元数据 + 核心指令 | 元数据启动时进入提示词；正文由 Agent 按需读取 |
| `scripts/` | 可执行脚本 | Agent 按指令需要时执行 |
| `references/` | 详细参考文档 | 需要深入信息时按需读取 |
| `assets/` | 模板、数据、schema | 需要时按需读取 |

### 2.2 `SKILL.md` 的两段结构

前一段是 `---` 包围的 YAML frontmatter，后一段是 Markdown 正文，也就是 Skill 被激活后 Agent 实际执行的“剧本”。

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 小写字母、数字、连字符，1–64 字符；不能以连字符开头/结尾，不能有连续连字符；必须与父目录名一致 |
| `description` | 是 | 功能与适用场景，最大 1024 字符 |
| `license` | 否 | 许可证名称 |
| `compatibility` | 否 | 环境要求，最大 500 字符 |
| `metadata` | 否 | 字符串键值对，如 author、version |
| `allowed-tools` | 否 | 空格分隔的预授权工具列表，实验性字段 |

`allowed-tools` 在 Deep Agents 0.7.10 中只是展示在技能提示词里，**不是工具注册或强制授权机制**。Skill 引用的 `fetch_url` 等工具仍需应用提供，权限必须由工具、Backend 或审批机制执行。

`description` 是 Agent 选择 Skill 的主要依据之一（启动列表还含名称和路径），写得模糊会造成漏召回或误召回。好的写法明确触发条件，例如“当用户要求审查代码质量、安全性或性能时使用此技能”；差的写法如“A helpful skill for developers.”。

### 2.3 Progressive Disclosure 三级加载

| 层级 | 内容 | 时机 | 处理者 |
|---|---|---|---|
| Level 1 Metadata | name + description + 文件路径 | Agent 启动 | `SkillsMiddleware` |
| Level 2 Instructions | `SKILL.md` 正文 | Agent 判断相关后调用 `read_file` | LLM 决策 + 文件工具 |
| Level 3 Resources | `scripts/`、`references/`、`assets/` | 指令引用到某个文件时 | LLM 自行决定 |

启动阶段，`SkillsMiddleware` 扫描配置目录下的 Skill 子目录，从 frontmatter 提取 name 和 description，把元数据和文件路径加入系统提示词，**正文不进入上下文**。用户请求到来后，Agent 根据 description 判断是否需要，再调用 `read_file` 读取 `SKILL.md`；正文经工具返回进入上下文。之后若指令引用了参考文件，Agent 再自行决定是否读取。注意：扫描阶段仍会读文件以解析元数据，“只读 frontmatter”指的是只有元数据进入模型上下文。正文建议控制在 5000 tokens / 500 行以内，超出部分拆到 `references/`。

为什么这样设计：

1. **省 token**：20 个 Skill 启动时只加载 20 条 description（几百 token），而不是 20 份完整指令（可能数万 token）。
2. **精准匹配**：只有当前任务需要的 Skill 才被加载，减少无关指令干扰。
3. **降低扩展开销**：Skill 数量增加时，启动开销主要随元数据增长；但大量或冗长的 description 仍会占上下文。

0.7.10 还有一个缓存边界：如果 state 已存在 `skills_metadata`（包括空列表），`before_agent` 会跳过再次扫描。因此同一 checkpoint 线程下一轮不一定发现新增 Skill；验证新发现要用新线程。

### 2.4 三种 Backend 与多源优先级

| Backend | 存储位置 | 适用 |
|---|---|---|
| `FilesystemBackend` | 本地磁盘 | 本地开发、CLI |
| `StateBackend` | LangGraph agent state | 无磁盘环境、动态注入 |
| `StoreBackend` | LangGraph Store | 跨线程、生产多用户共享 |

`skills` 参数接受**目录列表**，每个路径指向包含 Skill 子目录的父目录，例如 `skills=["/skills/"]` 对应 `/skills/myskill/SKILL.md`；传 `skills=["/skills/myskill.md"]` 不符合目录扫描约定，参数也不会替你创建目录或文件。多源同名时 **last wins**，后面的覆盖前面的，因此可以按“组织级 → 团队级 → 项目级”分层覆盖。`skills` 是普通 Python 列表，可按用户角色、租户、请求上下文或环境变量在运行时构造。

### 2.5 子 Agent 继承与权限

- **通用子 Agent** 自动继承主 Agent 的所有 Skills。
- **自定义子 Agent** 不继承，必须在定义时用 `skills` 字段显式指定。
- **元数据状态隔离**：各 Agent 分别加载自己的 `skills_metadata`，但这不是文件访问隔离；共用 Backend / Store namespace 时文件仍可共享，修改可能影响其他 Agent。

权限要分三个维度：可见性、写入权限、审批流程。`FilesystemPermission` 的 `mode="deny"` 可禁止对 `/skills/**` 的写入，Agent 仍能发现和读取；`mode="interrupt"` 则在写入前暂停，交人类审批，需要 Checkpointer，且要求 `deepagents>=0.6.8`。

### 2.6 用 Skills 执行代码

- **沙箱脚本**：`scripts/` 下的脚本可从任何后端读取，但执行需要支持沙箱的后端（如 DaytonaSandbox）。若 Skill 文件在沙箱外，需要用自定义中间件在 `before_agent` 上传、`after_agent` 下载。
- **解释器**：`CodeInterpreterMiddleware(mode="call")` 在内存中运行 JavaScript（QuickJS），适合循环、批量筛选、聚合等确定性逻辑。`skills` 配置只暴露提示词与资源，**不会自动向解释器导入辅助函数**；`mode="call"` 每次执行都是全新环境。早期通过 `await import("@/skills/<name>")` 导入 Skill 脚本的机制自 `langchain-quickjs 0.2.0` 起已移除。复用业务逻辑应使用 `ptc` 白名单（程序化工具调用）或沙箱完整脚本。运行需 `deepagents[quickjs]`（`langchain-quickjs 0.3.9` 核对）与 Python `>=3.11`。

### 2.7 Skills、Memory 与 Tools

| | Skills | Memory（`AGENTS.md`） | Tools |
|---|---|---|---|
| 用途 | 按需加载的领域能力 | 启动时加载的持久上下文 | 可调用的编程操作 |
| 时机 | 判断相关时才读 | 启动时 | 每轮可用 |
| 格式 | 目录中的 `SKILL.md` | `AGENTS.md` | 绑定到 Agent 的函数 |
| 场景 | 任务专属、可能很大的指令集 | 始终相关的全局规范 | 需要执行操作 |

决策规则：所有对话都需要的放 Memory；特定任务才需要的专业指令放 Skills；需要执行的原子操作做成 Tools。二者是连续光谱，Agent 也可以在工作中更新自己的 Skills，使其成为“渐进式披露的记忆”。

## 3. 代码与配置要点

### 3.1 一个完整的 `SKILL.md`

```markdown
---
name: langgraph-docs
description: Use this skill for requests related to LangGraph in order to fetch relevant documentation to provide accurate, up-to-date guidance.
---

# langgraph-docs

## Overview
This skill explains how to access LangGraph documentation to help answer questions and guide implementation.

## Instructions

### 1. Fetch the documentation index
Use the fetch_url tool to read the following URL:
https://docs.langchain.com/llms.txt

### 2. Select relevant documentation
Based on the question, identify 2-4 most relevant documentation URLs from the index. Prioritize:
- Specific how-to guides for implementation questions
- Core concept pages for understanding questions
- Tutorials for end-to-end examples
- Reference docs for API details

### 3. Fetch and synthesize
Use the fetch_url tool to read the selected documentation URLs, then answer the user's question.
```

### 3.2 本地磁盘后端

```python
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

backend = FilesystemBackend(root_dir="./my-project", virtual_mode=True)

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    backend=backend,
    skills=["/skills/"],
)
```

`/skills/` 相对于 Backend 根目录，即 `./my-project/skills/`；路径统一用正斜杠。本地磁盘后端建议显式 `virtual_mode=True`，与第 3 章的路径沙箱一致。

### 3.3 StateBackend 与 StoreBackend

```python
from deepagents.backends import StateBackend
from deepagents.backends.utils import create_file_data

skills_files = {
    "/skills/langgraph-docs/SKILL.md": create_file_data(skill_content),
}
agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    backend=StateBackend(),
    skills=["/skills/"],
    checkpointer=checkpointer,
)
result = agent.invoke(
    {"messages": [{"role": "user", "content": "What is langgraph?"}],
     "files": skills_files},
    config={"configurable": {"thread_id": "12345"}},
)
```

StateBackend 的虚拟路径必须以 `/` 开头，内容必须经 `create_file_data()` 格式化，直接传原始字符串会报错；文件通过 `invoke` 的 `files` 字段注入，同一 `thread_id` 后续调用可复用。

```python
store.put(
    namespace=("filesystem",),
    key="/skills/langgraph-docs/SKILL.md",
    value=create_file_data(skill_content),
)
```

StoreBackend 用 `store.put()` 写入一次即可跨线程访问；`namespace` 是接收 `Runtime` 并返回元组的函数。`InMemoryStore` 只在进程内有效，跨进程要换持久化 Store。

### 3.4 只读 Skill 库

```python
backend=CompositeBackend(
    default=StateBackend(),
    routes={
        "/skills/": StoreBackend(namespace=org_skill_namespace),
    },
),
permissions=[
    FilesystemPermission(
        operations=["write"],
        paths=["/skills/**"],
        mode="deny",
    ),
],
```

`deny` 会拦截 `write_file`、`edit_file` 和 v0.7 默认提供的 `delete`。这里最容易错的是 Store key：`CompositeBackend` 收到 `/skills/test-skill/SKILL.md` 会先剥离 `/skills/`，再把 `/test-skill/SKILL.md` 交给内部 `StoreBackend`。因此预填 key 应写 `/test-skill/SKILL.md`；写成 `/skills/test-skill/SKILL.md` 会被外层补回前缀，暴露成 `/skills/skills/...`。只有直接把 `StoreBackend` 当根 Backend 时才保留完整路径。

### 3.5 自定义子 Agent 的 Skills

```python
research_subagent = {
    "name": "researcher",
    "description": "Research assistant with specialized skills",
    "system_prompt": "You are a researcher.",
    "tools": [web_search],
    "skills": ["/skills/research/", "/skills/web-search/"],
}
```

主 Agent 的 `/skills/main/` 只自动传给通用子 Agent；`researcher` 的发现列表只有自己声明的两个目录，但这不禁止它用文件工具读取 `/skills/main/`。

## 4. 实验观察

配套 Notebook 构造一个最小 `release-check` Skill：`SKILL.md` 正文写“只按参考清单列出检查项”，`references/checks.md` 写“单元测试通过”和“构建产物中没有调试日志”。两句业务文字分别作为“正文已进入上下文”和“参考文件已进入上下文”的观察标记。默认 offline 用脚本模型发出 `read_file`，Skills 中间件、文件工具和消息循环真实执行。

实测三次模型请求的披露顺序：

| 请求 | 实际上下文 |
|---|---|
| 第 1 次 | 仅技能目录：`release-check`、description、`/skills/release-check/SKILL.md` |
| 第 2 次 | 目录 + `SKILL.md` 正文（含参考文件路径） |
| 第 3 次 | 目录 + 正文 + `references/checks.md` 的两条检查项 |

工具输出也是真实文件内容，首行带 `@@ lines 1-8 of 8 @@` 范围头；读取参考文件后，最终答复才包含那两条检查项。首次请求中的技能目录片段形如：

```text
- **release-check**: 当用户需要发布前检查清单时，读取本地资料并列出检查项。
  -> Read `/skills/release-check/SKILL.md` for full instructions
```

删除参考文件后用**新的 Agent 和空对话**重跑，第二个 `read_file` 返回真实错误：

```text
Error: File '/skills/release-check/references/checks.md' not found
```

最终答复报告缺失路径并停止，没有复用上一轮的检查项。Notebook 的 `check_disclosure` 不只比对最终答复，还按调用 ID 核对：第一次请求不能含正文或检查项；正文标记只能在该 `read_file` 返回后的请求中出现；参考标记只能在参考文件返回后出现；失败场景所有请求都不含检查项。

坑与边界：

- offline 脚本模型按公开规则读取文件，**不证明真实模型会自主选择 Skill**；随附输出不代表模型能力。
- 该实验 live 记录为 `Qwen/Qwen3-Coder-30B-A3B-Instruct` 于 2026-10-06 整本通过（版本 `ae27a77`），但真实模型仍可能改参数措辞。
- frontmatter 能解析，不保证它引用的资料存在；缺失文件只能由实际读取工具报错暴露。
- 临时目录由 `TemporaryDirectory` 清理；重跑必须从第一格创建新目录和新 Agent，不能借用旧对话里已读到的内容。
- Notebook 执行版本为 `deepagents==0.7.22`，正文部分行为按 0.7.10 核对。

## 5. 常见误区

- **把 Skill 当工具**：Skill 是流程与知识包，执行仍依赖 `read_file`、`fetch_url`、脚本或解释器。
- **以为 `skills=` 会自动创建文件**：它只声明扫描目录；文件必须由 Backend 或 Store 预先提供。
- **把文件路径当扫描目录**：`skills=["/skills/myskill.md"]` 不会被识别，应传父目录。
- **以为正文会在启动时全部注入**：只有元数据进入提示词，正文要 Agent 主动 `read_file`。
- **把 `allowed-tools` 当授权**：它不注册工具、不强制权限。
- **以为自定义子 Agent 会继承 Skills**：只有通用子 Agent 自动继承。
- **Store key 重复加挂载前缀**：会产生 `/skills/skills/...`。
- **以为 skill 元数据隔离等于文件隔离**：共用 Backend / namespace 时文件仍可互相读取和修改。
- **以为解释器能直接 import Skill 脚本**：该机制自 `langchain-quickjs 0.2.0` 起已移除，应使用 PTC 或沙箱。
- **Skill 越多越好**：大量重叠 description 会增加误匹配和选择困难，应定期合并、删除、拆分。

## 6. 自测清单

- [ ] 能写出一个符合规范的 Skill 目录，并解释 `SKILL.md` frontmatter 各字段的约束，尤其是 `name` 与父目录一致、`description` 的作用。
- [ ] 能按顺序说明三级渐进披露分别加载什么、由谁触发，以及为什么正文不预先注入。
- [ ] 能根据场景选择 `FilesystemBackend`、`StateBackend`、`StoreBackend`，并写出对应的 Skill 注入代码。
- [ ] 能解释多源 Skills 的 last-wins 规则、通用/自定义子 Agent 的继承差异，以及元数据隔离为何不是访问隔离。
- [ ] 能说明 `mode="deny"` 与 `mode="interrupt"` 的差别，并正确写出 `CompositeBackend` 路由下的 Store key。
