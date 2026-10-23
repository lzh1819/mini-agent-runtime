# Mini Agent Runtime

> **Pi-first · Python · Agent Runtime Architecture Research**\
> 以 [Pi](https://github.com/earendil-works/pi) 为主架构参考，使用 Python 独立重建 Agent Runtime 的核心机制，通过源码阅读、可重复实验和设计记录理解 Agent Harness。

**项目定位：技术研究与学习仓库，不是持续堆功能的 AI 产品。**

学习协作规则见 [`AGENTS.md`](AGENTS.md)，学习过程见 [`LEARNING.md`](LEARNING.md)。每一步按一个可讲清楚的教学目标组织，将实现或实验、验证和学习记录一起提交到 Git；提交说明引用对应学习条目。

## 1. 研究目标

本项目关注三件事：

1. **理解原理**：读懂 Pi 如何组织模型协议、Agent Loop、工具执行、运行状态、Session 和 Context。
2. **独立实现**：以 Python 实现一个尽量小、行为可测试的 Pi 风格 Agent Runtime；对齐职责与语义，不逐行翻译 TypeScript。
3. **形成判断**：通过代码和实验解释 Pi 的设计选择，并记录 Python 实现的收益、限制与取舍。

最终成果不仅是一个能调用工具的 Agent，更包括**源码地图、架构决策、行为测试和实验结论**。

### 核心原则

- **Pi 是唯一主架构基准**。OpenAI Agents SDK、mini-swe-agent、learn-claude-code 仅作为专题对照，不拼装“四框架混合架构”。
- **先理解边界，再决定功能**。保留模型协议层、执行内核、会话 Harness 的责任分离；不追求 Pi 的全部特性。
- **先验证语义，再追求体验**。优先使用 Fake Model 和确定性测试，不以真实模型输出的偶然成功代替正确性。
- **源码事实、设计解释、个人判断分开记录**。每一项重要架构建议尽可能对应固定 Commit、源码位置与实验。
- **架构对齐不等于文件对齐**。可以用 Python 惯用方式组织代码，但不能无意改变所研究机制的含义。

## 2. 研究基线与当前状态

本项目的 Pi 专项研究基于固定版本：

- **仓库**：[`earendil-works/pi`](https://github.com/earendil-works/pi)
- **Commit**：[`7c22a93d0ed02092142fa63823e63d76aa4e2406`](https://github.com/earendil-works/pi/commit/7c22a93d0ed02092142fa63823e63d76aa4e2406)
- **研究日期**：2026-10-22
- **研究报告**：[`research/pi-source-study.md`](research/pi-source-study.md)

**状态**：R0 核心源码路径研究已形成报告；V0 Python 实现尚待开始。R0 审阅了核心执行链及部分测试断言，且仅对 Pi 的原始 EventStream 模块做过 5 项隔离运行验证；**没有完成全仓库审计或跑通 Pi 全量测试**。后续每个专题仍需回到源码继续核对。

Python 构建配置与基础包目录已经存在，包骨架已整理。当前可以构建、安装并导入 `mini_agent_runtime`，但尚无模型协议、Provider、AgentLoop、工具或 CLI。包构建验证不代表 Agent Runtime 已实现。

此 README 是当前研究方案的**执行基线**，可随着新的源码证据和实验结果修订；变更应记录原因，不随意重排阶段。

## 3. Pi-first 架构蓝图

```text
CLI / SDK / 其他入口
        |
        v
AgentSession                         # 应用层 Harness
  |-- SessionManager                 # 历史记录、分支、投影、持久化
  |-- Context / Compaction           # 请求上下文与压缩策略
  |-- Coding Tools / Extensions      # 具体工具和扩展策略
  `-- Agent
        |
        v
Agent + AgentLoop                    # 通用执行内核
  |-- Agent State / Event Lifecycle
  |-- Tool Execution Pipeline
  `-- Steering / Follow-up / Abort
        |
        v
pi-ai                                # 模型协议层
  |-- Message / Tool / Model Types
  |-- Transcript Normalization
  |-- Model Event Stream
  `-- Provider Adapter
        |
        v
LLM Provider
```

这是从 Pi 选定的 Coding Agent 核心调用路径归纳的**职责图**，不是全部包的依赖图。Harness 也可直接使用模型层执行摘要等操作。

| Pi 角色 | 主要责任 | Python 对应模块 |
|---|---|---|
| `pi-ai` | 模型与消息协议、Provider 适配、模型事件流 | `mini_agent_runtime.ai` |
| `AgentLoop` | 模型请求、工具调用调度、Turn 与停止条件 | `mini_agent_runtime.agent.loop` |
| `Agent` | 状态、事件监听、排队、取消、禁止重入 | `mini_agent_runtime.agent.agent` |
| `AgentSession` | 组合内核、上下文、持久化和恢复策略 | `mini_agent_runtime.harness.session` |
| `SessionManager` | 追加式会话条目、树形分支、上下文投影 | `mini_agent_runtime.harness.session_manager` / `projection` |
| Compaction | 估算、切分、摘要和上下文检查点 | `mini_agent_runtime.harness.compaction` |

### 必须区分的三种数据

```text
Session Entries（可追溯的原始记录）
    -> Session Projection（当前分支的有效上下文视图）
    -> LLM Messages / Provider Payload（实际模型输入）
```

**不把原始 Session History 当成直接发送给模型的 Messages；不把 Context Compaction 写成删除历史。**

## 4. 仓库组织

### 当前包骨架与构建验证

```text
pyproject.toml                  # 项目元数据、构建后端和开发依赖
src/
└── mini_agent_runtime/
    ├── __init__.py              # 包说明；导入包时执行的入口
    └── py.typed                 # 向类型检查工具声明包提供类型信息
```

`[project].name` 中的 `mini-agent-runtime` 是发行项目名称，用于安装和查询元数据；`mini_agent_runtime` 是 Python 导入包名。当前 `uv_build` 默认将规范化后的项目名称用于查找 `src/mini_agent_runtime/`，无需增加额外配置。这个对应关系来自构建后端的约定，Python 的 `import` 本身不负责把连字符转换为下划线。

`[build-system]` 指定构建后端 `uv_build` 及其版本范围；`[dependency-groups].dev` 中的 pytest 用于开发，不是包的运行时依赖。`src/` 将源码与仓库根目录分开；安装操作使包在相应 Python 环境中可导入，当前工作目录包含仓库本身并不等同于已安装包。

在项目根目录执行以下实验，需要已安装 uv 和可用的 Python 3.11。临时虚拟环境用于验证实际 wheel，不依赖项目已有的可编辑安装：

```bash
uv build --wheel
check_dir="$(mktemp -d /tmp/mini-agent-runtime-check.XXXXXX)"
uv venv "$check_dir/venv" --python 3.11
uv pip install --python "$check_dir/venv/bin/python" dist/mini_agent_runtime-0.1.0-py3-none-any.whl
cd "$check_dir"
./venv/bin/python -I -c "import mini_agent_runtime; from importlib.metadata import version; print(mini_agent_runtime.__file__); print(version('mini-agent-runtime'))"
```

预期输出为临时虚拟环境 `site-packages` 下的 `mini_agent_runtime/__init__.py` 路径，以及 `0.1.0`。`-I` 排除当前目录和 `PYTHONPATH` 对导入的影响。实验完成后当前 shell 位于临时目录，可使用 `cd -` 返回项目目录。具体实验结果见 [`LEARNING.md`](LEARNING.md) 的 L003。

### 演进目标结构

以下是**演进目标结构**；按里程碑渐进创建目录和文件，不需要 V0 就生成全部空模块。

```text
mini-agent-runtime/
├── README.md
├── AGENTS.md                       # 学习协作与提交约定
├── LEARNING.md                     # 学习过程与实验记录
├── pyproject.toml                  # 已有构建配置
├── src/mini_agent_runtime/
│   ├── __init__.py                  # 已有包入口
│   ├── py.typed                     # 已有类型信息标记
│   ├── ai/                         # 协议 / 模型适配
│   │   ├── messages.py
│   │   ├── events.py
│   │   ├── provider.py
│   │   └── providers/openai.py
│   ├── agent/                      # Agent Core
│   │   ├── loop.py
│   │   ├── agent.py
│   │   ├── events.py
│   │   └── tools.py
│   ├── harness/                    # AgentSession / SessionManager
│   │   ├── session.py
│   │   ├── session_manager.py
│   │   ├── projection.py
│   │   ├── compaction.py
│   │   └── tools/read_file.py
│   └── cli.py
├── tests/                          # 使用 Fake Model 的确定性测试
├── experiments/                    # 小实验与结果记录
├── research/
│   ├── pi-source-study.md          # R0 源码研究报告
│   ├── references.md               # 参考仓库及版本记录
│   ├── invariants.md               # 逐步维护的行为不变量（待创建）
│   └── decisions/                  # 研究阶段逐步形成的 ADR（待创建）
└── references/                     # 参考源码（Git submodules）
    ├── pi/
    ├── openai-agents-python/
    ├── mini-swe-agent/
    └── learn-claude-code/
```

**研究文档、参考源码和自己的 Python 实现严格分开**。`references/` 不作为项目运行时依赖，也不直接修改第三方源码来充当自己的实现。

### 参考仓库管理

| 仓库 | 角色 | 地址 |
|---|---|---|
| **Pi** | 主架构与行为语义基准 | [earendil-works/pi](https://github.com/earendil-works/pi) |
| OpenAI Agents SDK (Python) | 对照运行接口、工具和状态边界 | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) |
| mini-swe-agent | 对照极简循环与执行环境 | [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) |
| learn-claude-code | 对照逐层递进的 Harness 教学实现 | [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) |

推荐采用 **Git submodule**，将第三方源码固定到 Git 所记录的 Commit，而不是复制源码或始终跟踪 `main`。在一个**尚未添加 submodule 的新研究仓库**中执行一次：

```bash
git submodule add https://github.com/earendil-works/pi.git references/pi
git submodule add https://github.com/openai/openai-agents-python.git references/openai-agents-python
git submodule add https://github.com/SWE-agent/mini-swe-agent.git references/mini-swe-agent
git submodule add https://github.com/shareAI-lab/learn-claude-code.git references/learn-claude-code

git -C references/pi checkout 7c22a93d0ed02092142fa63823e63d76aa4e2406
git add .gitmodules references/
git commit -m "research: pin reference repositories"
```

其他三个项目在首次专题对照时记录所用的 Commit，**本 README 不假称它们已完成源码审阅**。克隆已经设置好 submodule 的研究仓库后，运行：

```bash
git submodule update --init --recursive
```

## 5. 研究与实现路线

里程碑不是功能列表，而是按 **Pi 架构依赖关系和可验证行为** 排列的研究主题。

| 阶段 | 核心研究对象 | 产出与验收重点 |
|---|---|---|
| **R0** | Pi 核心源码地图与契约 | 角色边界、调用链、源码索引；已形成核心报告 |
| **V0** | Messages → Fake Provider → AgentLoop → Tool Calling | 最小纵向闭环；事件、错误和工具 ID 测试 |
| **V1** | Stateful Agent / Event Lifecycle | 状态、监听器等待、运行互斥、Steering、Follow-up、Abort |
| **V2** | Session Tree / Context Projection | 内存会话树、分支、编辑与投影；再接 JSONL |
| **V3** | AgentSession / Harness Composition | 事件驱动的持久化、请求前投影、低层与上层生命周期 |
| **V4** | Compaction / Context Checkpoint | 合法切分、摘要、重复压缩、上下文重建 |
| **V5（可选）** | Extension / Multi-provider / Parallel Tools | 按研究问题决定，不默认展开功能清单 |

### V0：Pi 风格的最小执行闭环

**范围**：基础消息类型、最小模型事件、Fake Provider、AgentLoop、一个限定工作目录的只读工具、基础 Agent 事件。初版允许顺序执行工具；完整 Streaming UI、Session 持久化和摘要均不纳入。

**完成标准**：确定性测试覆盖直接回答、一次及多轮 Tool Calling、同一响应的多个工具调用、未知工具、非法参数、模型报错、`length` 截断时不执行工具、达到执行预算停止；检查事件顺序、工具调用 ID、下一次模型输入及模型调用次数。

### V1：Agent 是有生命周期的状态对象

实现当前 History、未完成的 Streaming 状态、订阅事件的顺序语义、禁止同一 Agent 同时执行两次、Steering/Follow-up 队列和协作式 Abort。

**完成标准**：监听器未完成不得提前报告 idle；消息入队不立即写入 History；队列在规定边界消费；运行中重入被拒绝；取消不声称回滚工具副作用。

### V2：先 Session Tree，后完整 Context 管理

先以内存实现 `id` / `parent_id` / `leaf`、分支和 `SessionProjection`，再实现 JSONL 保存与加载。区分原始条目和模型可见投影，允许以编辑条目替换或隐藏投影内容但不覆盖原始记录。

**完成标准**：分支独立、来源可追溯、原始记录不变、保存及重新加载后的分支选择规则明确。注意仅移动内存 `leaf` 不等于自动持久化分支选择。

### V3：将 Core 与 Harness 正确组合

用 AgentSession 订阅 Agent 关键事件，在完成消息的边界写入会话条目，在 `prepareRequest` 读取当前权威 Projection，并分清 `agent_end` 与上层 session settled。

**完成标准**：一条完成消息只保存一次；请求使用正确投影；模型失败产生可观察记录；无需 CLI 就能进行单会话端到端测试。

### V4：上下文压缩是非破坏性的检查点

研究 Token 估计、切分点、摘要生成、Compaction Entry 和投影重建。先验证确定性的切分与保留逻辑，再评估真实模型的摘要质量。

**完成标准**：不切断 Tool Call / Tool Result 关系；错误或不完整摘要不得提交为正常检查点；重复压缩不叠加过时摘要；原始历史保留；摘要基于当前有效投影。

### V5：仅在研究问题明确时扩展

例如并行工具的“完成事件顺序”和“消息记录顺序”、多 Provider 适配差异、Hook 设计、Extensions。**不因 Pi 支持某功能就要求自己实现某功能。**

## 6. 每个专题怎样研究

遵循一个可重复的闭环：

```text
定义具体问题
    ↓
阅读 Pi 对应源码与测试（固定 Commit）
    ↓
绘制调用链 / 数据流，写出行为不变量
    ↓
设计 Python 最小实现与 Fake Model 测试
    ↓
运行实验，记录成功、失败与边界行为
    ↓
必要时对照其他项目
    ↓
形成 ADR / 研究笔记，决定保留或裁剪
```

每完成一个机制，回答：

1. **它解决了什么具体问题？** 如果缺少它，会出现什么可复现的错误或限制？
2. **Pi 是怎样实现的？** 关联具体文件、函数、消息结构与测试；必要时对照 OpenAI Agents SDK 等。
3. **为什么这样设计？我的 Python 实现有什么取舍？** 区分源码事实、官方明确的设计理由和自己的推断。

推荐的研究笔记字段：`问题 → 源码证据（Commit/路径/函数）→ 预期行为 → 实验 → 结论与取舍 → 未解决问题`。不要把未经测试的推断写成已验证事实。

## 7. 跨版本必须维护的行为不变量

- 模型请求与工具执行通过明确的协议边界连接；工具声明不携带可执行 Python 函数。
- Tool Call 和 Tool Result 有可核对的 ID；非法、未知或失败工具调用不被悄悄当成成功。
- 被 `length` 截断的响应不执行可能不完整的工具调用。
- 完成消息与流式中间态分离；关键监听器的完成语义有明确约定。
- Agent 的 Turn、Run、Idle 和 AgentSession 的 settled 不混为一谈。
- 原始 Session 条目不因分支、编辑或 Compaction 而被无意删除。
- Provider 收到的输入由**当前有效投影**构建，而不是盲目发送全部存储记录。
- Context 切分和恢复不制造孤立的工具结果。
- Abort 是协作式取消，不代表外部工具已完成副作用的自动回滚。

不变量应随着每个版本转换成可执行测试。**Fake Model 测试是主要正确性依据；真实 Provider 测试只用于验证适配和模型交互，不替代确定性测试。**

## 8. 非目标与安全边界

本项目**不以**以下内容作为基础里程碑：复刻 Pi 完整 UI/RPC/扩展生态；覆盖所有模型供应商；构建多 Agent 平台；实现完整审批产品；引入复杂数据库或分布式调度；追求生产级性能和可用性。

测试工具默认应只对**无敏感数据的临时工作区**开放最小权限。初期不默认开放 Shell、网络访问或任意文件写入。Schema 校验和 Tool Hook 不是 OS 沙箱；需要真正的隔离时，应在独立进程、容器或其他沙箱中执行。

## 9. 开始学习

1. 阅读 [`research/pi-source-study.md`](research/pi-source-study.md)，先理解 `AgentSession → Agent → AgentLoop → pi-ai` 的责任边界。
2. 按 [`research/references.md`](research/references.md) 核对参考仓库和固定 Commit；从 Pi 的消息协议、`agent-loop.ts`、`agent.ts` 以及相应测试开始。
3. 从 **V0 的数据契约与 Fake Provider** 开工；先写失败用例，再实现最小闭环。
4. 每完成一个里程碑，更新研究记录、行为不变量和 ADR，再开始下一主题。

当前构建配置要求 **Python 3.11+**，已声明 pytest 开发依赖；V0 计划采用异步协议（`async def` / `AsyncIterator`）。现有包骨架支持构建与导入，执行内核将在 V0 实现。

## 10. 核心源码入口

以下链接固定到研究 Commit，避免默认分支变化造成结论漂移：

- [pi-ai：消息与模型类型](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/ai/src/types.ts)、[Transcript 规范化](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/ai/src/utils/transcript.ts)
- [Agent Core：AgentLoop](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/src/agent-loop.ts)、[Agent](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/src/agent.ts)、[Core 类型](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/src/types.ts)
- [Harness：AgentSession](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/agent-session.ts)、[SessionManager](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/session-manager.ts)、[Compaction](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/compaction/compaction.ts)
- [测试：Agent Loop](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/test/agent-loop.test.ts)、[Agent 生命周期](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/test/agent.test.ts)、[Session Context Edit](https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/test/session-context-edit.test.ts)

---

**项目成功标准：不是“复刻了 Pi 的多少功能”，而是能够依据源码与实验，准确解释每个已实现机制的责任、执行时机、不变量以及 Python 版本的取舍。**
