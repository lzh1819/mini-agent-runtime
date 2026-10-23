# Pi Agent Runtime 源码架构研究报告

## 研究基线与结论

- 研究日期：2026-10-22。
- 仓库：`earendil-works/pi`。
- 固定 Commit：`7c22a93d0ed02092142fa63823e63d76aa4e2406`。
- 目标：以 Pi 的核心架构为蓝图，使用 Python 重建一个可测试、可解释的学习型 Agent Runtime。
- 方法：沿真实调用关系阅读实现、核对相关测试断言，并对一个可独立运行的原始模块执行隔离验证。

这个 Commit 的提交时间是 2026-10-22T07:02:01Z。本文讨论固定版本，不把结论泛化为所有历史版本或未来版本。[S01]

**核心结论：应该保留 Pi 的协议层、执行内核、会话 Harness 三个职责层次；第一版就理解消息与事件契约，先打通最小纵向执行链，再引入会话树、上下文投影和压缩。**

这不是把 Pi 每个文件逐行翻译成 Python，也不是把四个参考项目混合成一个框架。Pi 决定主架构；其他项目只用于解释与比较特定取舍。

### 覆盖范围与验证限制

重点审阅了 `pi-ai` 的协议与事件流、一个 Provider 适配入口，`pi-agent-core` 的循环与状态包装，以及 `pi-coding-agent` 的组合入口、关键 AgentSession 路径、会话投影、压缩和工具接入。

`agent-loop.ts`、`messages.ts`、`utils/event-stream.ts`、`utils/transcript.ts` 等核心文件已阅读；较大的 `agent-session.ts`、`session-manager.ts`、压缩文件和测试文件按相关执行路径选读。附录给出实际覆盖范围。

**没有完成全仓库审计，没有运行 Pi 全部测试，没有执行真实模型端到端任务。** 当前环境通过 GitHub connector 读取源码成功，但容器 Git 拉取因域名解析失败，无法获得完整依赖环境运行仓库测试。实际运行验证仅包括原始 `EventStream` 模块的 5 个自编隔离测试。

仓库还包含 `durable`、`chord`、`telemetry`、`tui` 等包。下面的架构图表示本项目选定的 Coding Agent 核心路径，不是完整 monorepo 依赖图。这些额外系统没有在本轮全面审阅。[S22]

---

## 1. 架构地图：首先分清四个核心角色

```text
交互入口 / SDK 使用者
          │
          ▼
AgentSession                         Harness 层
  ├── SessionManager                 历史、分支、投影、存储
  ├── Compaction                     压缩准备与摘要生成
  ├── Coding Tools / Extensions      领域工具与扩展策略
  └── Agent                          状态化运行对象
        │
        ▼
Agent + AgentLoop                    Agent Core 层
  ├── 运行状态、队列、取消、事件
  ├── 模型与工具之间的执行调度
  └── 通用工具执行管线
        │
        ▼
pi-ai                               模型协议层
  ├── Message / Tool declaration / Model / Usage
  ├── Transcript 规范化和重放
  ├── AssistantMessageEventStream
  └── Provider 协议转换
        │
        ▼
模型服务
```

这是根据 `sdk.ts` 的组合代码、Agent 的构造与执行，以及 AgentSession 安装的回调归纳的职责图。具体依赖不一定严格只有相邻层：Harness 也会调用模型层进行摘要等工作。[S07][S08][S09][S10][S15]

| 角色 | 主要职责 | 不应误认为 |
|---|---|---|
| `AgentLoop` | 消费模型输出、执行工具、发出生命周期事件、判断下一轮 | 会话数据库、Coding Agent 全部业务 |
| `Agent` | 持有运行时状态，包装低层循环，管理队列、取消与监听器 | 自动具备持久化与任意崩溃恢复 |
| `AgentSession` | 组合 Agent、持久化、请求上下文、扩展、重试与压缩策略 | 一个仅用于保存聊天记录的容器 |
| `SessionManager` | 追加条目、维护树与当前叶节点、构建上下文投影、读写会话文件 | 执行 LLM 或实际执行工具的调度器 |

### 对 Python 架构的直接影响

`agent_loop.py` 不应负责选择会话文件、读取项目配置、生成摘要或保存 JSONL。它提供明确的执行时机，由上层注入策略。

`Agent` 和 `AgentSession` 应分开。没有磁盘、CLI 和完整 Harness 时，底层 Agent 仍应能用 Fake Model 与测试工具运行。这是本项目最值得保持的边界。

---

## 2. 模型协议层：Pi 不是只给 HTTP API 套一层函数

### 2.1 消息是带结构的记录，不只是字符串

Pi 的 `AssistantMessage` 不仅有文本，还可能包含工具调用、thinking 内容块与对应的不透明签名，并记录 provider、model、usage、stopReason 等元数据。工具调用带有 ID，工具结果通过 `toolCallId` 关联。[S02]

因此，Python 第一版可以只启用文本和工具调用，但不应把整个内部接口定成“输入字符串、返回字符串”。至少需要表达：

```text
SystemMessage / UserMessage
AssistantMessage(content blocks, stop_reason, usage)
ToolCall(id, name, arguments)
ToolResultMessage(tool_call_id, content, is_error)
```

Provider 特有的重放信息应留在适配层能够保留的位置。不能为了“统一接口”而丢弃未来继续对话所必需的数据。

### 2.2 研究版本采用 transcript-centric 的请求表示

在所选版本中，系统指令和工具声明可以作为 transcript 中的 system 消息保存。后续 system 消息可以更新命名指令区块及工具集合。

`getCurrentSystemMessage()`、`getCurrentTools()` 重放这些变化；`normalizeContext()` 把兼容入口的顶层 prompt/tools 转换为 transcript。Provider 不支持对话中途 system 更新时，适配层可以将它们折叠成当前有效的系统状态。[S03]

这意味着不能把旧示例中的 `{systemPrompt, messages, tools}` 当成当前整个内核的唯一规范表示。测试明确断言，底层循环交给 Provider 的 context 只有 `messages`。[S18]

**设计取舍判断：** 将指令与工具变化放入 transcript，有利于解释某个时间点的模型输入，但增加了重放、检查点和 Provider 兼容逻辑。Python 初期可只支持一个初始 system 状态，明确把中途变更列为暂不支持，而不另造一套无记录的可变配置。

### 2.3 模型输出是“事件流 + 最终响应”

`EventStream` 同时提供异步迭代与 `result()`。`AssistantMessageEventStream` 在 `done` 或 `error` 事件上产生最终 AssistantMessage。[S04]

Provider 适配入口负责请求构建、上游流处理及终止原因规范化。以已读的 OpenAI Responses 入口为例，它检查输出是否仍为 pending，避免把无终止原因的流直接视为正常完成。[S05]

Agent Core 的 `StreamFn` 契约要求：请求或模型运行失败通常通过流内的错误事件和最终错误消息表达，而不是任意向外抛异常。`Agent` 包装层还对异常路径进行兜底，形成错误消息与结束事件。[S06][S08]

**Python 建议：** 优先采用异步接口；第一版 Fake Stream 可以只产生最少的完整响应事件，不必立刻实现所有字符增量。但完整文本响应、错误终止和流结束应有不同语义。

还要注意，已读的 `Agent.runWithLifecycle()` 会捕获执行异常并转为错误消息/事件；在这些错误处理正常完成时，`await agent.prompt()` 本身可以正常返回。调用方应检查终止消息或状态，不能把 Promise 已完成等同于任务成功。[S08][S19]

---

## 3. 一次执行的真实调用链

### 3.1 初始化：`createAgentSession()` 是组合入口

`createAgentSession()` 恢复会话上下文与模型设置，构建 Agent，注入 `convertToLlm`、模型 stream 函数及 context transform，再组合成 AgentSession。[S09]

这属于“把部件装起来”的代码，不应成为底层循环的一部分。

### 3.2 正常工具调用路径

```text
AgentSession.prompt()
  └── 输入处理、模板/技能展开、运行状态与模型检查
      └── _startRun()
          ├── _prepareRun()
          └── Agent.prompt()
              └── runAgentLoop()
                  ├── 发出输入消息事件
                  ├── prepareRequest()
                  ├── transformContext()
                  ├── convertToLlm()
                  ├── normalizeContext()
                  ├── 调用 Provider Stream
                  ├── 完成 AssistantMessage，发出 message_end
                  ├── 校验并执行工具
                  ├── 完成 ToolResultMessage
                  ├── finishTurn()
                  ├── turn_end
                  └── 判断继续、处理队列，或 agent_end
```

对应实现位于 SDK、AgentSession 的 prompt/_startRun、Agent 的 runPromptMessages，以及 AgentLoop 的 runLoop/streamAssistantResponse。[S07][S08][S09][S10][S11]

三个回调时机不能混为一谈：

| 回调 | 时机 | 适合承担的责任 |
|---|---|---|
| `prepareRequest` | 每次模型请求前，包括第一次；待输入消息已发出相应事件 | 安装当前权威上下文投影 |
| `prepareNextTurn` | 确认还要继续之后、下一轮开始前 | 回合间压缩、刷新工具和运行配置 |
| `finishTurn` | assistant 与该轮工具结果完成后，`turn_end` 之前 | 结束或继续的策略判断 |

这些顺序既体现在实现，也有相关测试断言。`finishTurn: continue` 是确保下一轮请求，不是无论已有何种继续理由都再加一次请求。[S06][S07][S18]

### 3.3 重要修正：一次低层 Run 结束，不一定意味着用户任务已彻底结束

`AgentSession._startRun()` 在 `await agent.prompt()` 后仍有自己的协调逻辑：处理可重试错误、压缩恢复、排队任务和结束前边界，再决定是否调用 `agent.continue()`。最后才发出 `agent_settled`。[S11]

因此需要区分：

```text
模型响应结束
≠ 一轮模型与工具执行结束
≠ 低层 agent_end
≠ 所有异步监听器已完成
≠ 上层 AgentSession 已 settled
```

Python 项目应明确自己的 `run()`、`wait_for_idle()` 与会话活动结束分别承诺到哪一步。不能收到第一个 `agent_end` 就关闭存储或释放上层仍会使用的资源。

---

## 4. 事件与状态：不是“加日志”这么简单

`Agent.processEvents()` 先更新内部状态，再按注册顺序等待监听器。`message_end` 才把完成消息写入 Agent 的 messages；流式中间态由 `streamingMessage` 单独承载。[S08]

`agent_end` 是最后一个循环事件，但 Agent 直到异步监听器完成、`finishRun()` 执行后才变为空闲。测试专门断言：监听器仍在等待时，`prompt()` 不能提前完成，`waitForIdle()` 也不能提前返回。[S19]

这里还有三个不能混同的接口：

- 模型侧 `EventStream` 是带缓冲的异步事件流。
- `Agent.subscribe()` 的监听器会被等待，属于执行顺序的一部分。
- 已读的 AgentSession 公共 `_emit()` 直接调用监听器，不具有与 Agent.subscribe 完全相同的等待语义。[S04][S08][S10]

**设计取舍判断：** 被等待的事件链能保证关键处理完成后才推进，但慢监听器也会拖慢运行。Python 实现应把关键状态/持久化处理与不需要阻塞运行的界面通知区分，而不是所有回调一律丢进后台 Task。

### Turn 的含义

该版本 `AgentEvent` 类型明确：一个 turn 包含“一次 assistant response，加上它产生的工具调用与结果”。它不是单纯一个网络请求，也不是一次用户输入到最终回答的全过程。[S06]

因此建议使用明确的计数名字，例如 `model_calls`、`tool_calls`、`turn_index`，不把不同粒度都叫 step。

---

## 5. 工具执行：一个有边界的管线

### 5.1 声明、实现和执行结果是不同对象

模型层的 Tool declaration 描述名称、用途和参数 Schema；AgentTool 在其上增加可执行函数及运行策略。`toToolDeclaration()` 会去掉执行和展示相关字段，避免把可执行对象直接放入可持久化 transcript。[S03][S06]

实际文件工具位于 Harness，而不是在通用 AgentLoop 中写 `read_file` 特判。已读的 `read.ts` 还通过 `ReadOperations` 注入文件读取、访问检查等操作，便于替换执行环境或做测试。[S17]

### 5.2 管线顺序

```text
查找工具
→ 参数预处理
→ Schema 校验
→ beforeToolCall
→ 取消检查
→ execute
→ afterToolCall
→ 形成最终 ToolResultMessage
```

未知工具、参数错误、被阻止或工具抛异常都可以转换为与原调用关联的错误结果。当前类型也支持显式 `isError: true`；不能只在成功结果的文本里写“Error”，却不标记失败。[S06][S07]

`content` 是面向模型的输出；`details` 是日志/界面元数据；`structuredContent` 面向程序化调用者。三者不应默认全塞进上下文。[S06]

### 5.3 并行执行有两种顺序

这个固定版本的 Agent 默认允许 parallel。预检查按顺序进行，允许执行的工具并发运行；完成事件可按实际完成顺序发出，但最终工具结果消息按 assistant 原始调用顺序组织。批次中任何工具要求 sequential，就使该批次顺序执行。[S07][S08]

**Python 初期采用顺序执行是合理裁剪，但必须标注为裁剪，不要说成 Pi 的当前默认行为。**

工具结果的 `terminate` 也不是任意一个工具设置就停止：只有该批次所有已最终处理的结果都要求 terminate，才满足这条提前结束规则。[S07]

### 5.4 输出截断必须在执行工具前处理

模型响应以 `length` 结束时，Pi 不执行该响应中的工具调用，而生成失败结果。原因是工具参数即使被部分 JSON 解析器修复为可解析对象，仍可能不完整。[S07]

这应成为第一轮行为测试，而不是等接入写文件工具后才考虑。

### 5.5 Hook 不是隔离边界

Pi README 明确说明默认沿用启动进程权限，没有内置用于限制文件系统、进程、网络或凭据访问的通用权限系统。[S22]

因此 Python 学习版本仍应使用受限、无敏感数据的测试工作区，不默认开放 Shell。以 Pi 架构为蓝图，不要求复制其所有默认权限选择。工具输出限制也不等于内存、CPU 或操作系统级隔离。

---

## 6. 会话与上下文：Pi 架构中最值得深入研究的部分

### 6.1 会话不是一个直接发送给模型的 messages 数组

SessionManager 的条目包含 ID、parentId 和时间。追加条目时以当前 leaf 为父节点，再推进 leaf；`branch()` 则移动 leaf，下一次追加形成新路径。原路径仍存在。[S13]

`getBranch()` 获取原始分支，`buildSessionProjection()` 构建当前分支面向模型的投影，两者不是同一份数据。[S12][S13]

**一个容易漏掉的细节：仅移动 leaf 不等于持久化这次选择。** 已读的 `branch()` 只改变内存指针；`_buildIndex()` 加载文件时把最后读到的条目作为 leaf。下一次追加才会把新的父子关系写入历史。因此 Python 版本需要明确“重启后选择哪个分支”的规则，不能默认一次没有后续写入的游标移动也能自动恢复。[S13]

**推论：** 这里的分支只改变会话记录与后续模型输入。`branch()` 不执行文件系统回滚，因此不能假设回到旧节点就撤销此前工具产生的副作用。

### 6.2 上下文是一条可追溯的转换链

```text
Session 原始条目树
    ↓ 选取当前 leaf 的祖先路径
Active Branch
    ↓ 应用最新 compaction 的保留边界
Context Entries
    ↓ 应用 context_edit，并保留来源条目
Session Projection / AgentMessage[]
    ↓ transformContext
    ↓ convertToLlm
模型层 Message[]
    ↓ transcript 规范化与 Provider 转换
实际请求
```

`buildSessionProjection()` 保留 `sourceEntry → messages` 的来源关系。最新的 context_edit 可改变目标条目的模型可见内容，或用 null 使它不可见，而不覆写原始条目。[S12]

测试明确覆盖“只从投影中隐藏”“最新编辑覆盖早前编辑”“编辑只在当前分支生效”“原始内容仍保持不变”。[S20]

### 6.3 不同转换阶段解决不同问题

`buildSessionProjection()` 解决会话记录、分支、编辑与压缩的权威视图。

`transformContext()` 是对 AgentMessage 级别的请求视图再变换，例如上层扩展上下文处理。

`convertToLlm()` 把 Harness 的自定义消息转换为模型层消息。例如 branchSummary、compactionSummary、custom 会转换为 user 消息；部分 bashExecution 可明确排除。[S09][S12][S14]

这三个职责不应混在一个“删旧消息”的函数里。

另外，custom message 的 `display=false` 是界面显示属性，不自动意味着“不发给模型”；与纯自定义状态条目不参与 context 的行为不同。[S12][S14]

### 6.4 持久化不是把所有 Runtime Events 写入磁盘

AgentSession 在 `message_end` 保存完成消息，同时使用专门条目记录模型设置、压缩等状态。工具进度和字符增量事件不等于逐条写入的 SessionEntry。[S10][S13]

普通历史写入采用追加式设计；迁移或分支文件生成仍可能重写文件。不要把它描述为任何情况下文件都绝不修改。

这也不是任意执行点的事务恢复。如果外部工具完成了副作用，但结果未保存，恢复时仍需要独立的幂等或副作用核对机制。已读的 JSONL 写入代码不构成“恰好执行一次”的证明。[S13]

### 6.5 恢复失败响应也遵循“保留记录、修正视图”

AgentSession 的 `_omitRecoveryAttempt()` 对失败 assistant 与相应工具结果追加 context_edit 遗漏记录，之后重建投影，而不是直接删除原始响应。这样还避免了后续请求出现孤立工具结果。[S10]

---

## 7. Compaction：构建检查点，而不是破坏历史

所读实现将几个步骤分开：

```text
估算当前投影的上下文大小
→ 选定合法切分点
→ 准备待摘要内容和保留边界
→ 调用模型生成摘要
→ 返回 CompactionResult
→ SessionManager 追加 CompactionEntry
→ 重建下一次请求的模型上下文
```

压缩算法返回结果，SessionManager 负责记录，AgentSession 负责在适当生命周期边界触发与协调。[S10][S13][S15]

### 7.1 需要保留的四个不变量

**原始历史不因为压缩而被删除。** 后续请求使用摘要与保留内容，原条目仍可供回顾。

**工具结果不能脱离其调用。** 切分点可以是用户类或 assistant 消息，不能直接落在 toolResult 上。

**摘要必须基于当前有效投影。** 已经在 context_edit 中遗漏或替换的内容，不应重新从原始历史“复活”进摘要。

**不完整摘要不应作为正常检查点写入。** 实现对错误和 length 截断进行失败检查；某些摘要路径也拒绝模型尝试调用工具。[S12][S15]

### 7.2 重复压缩和超长单次任务并不简单

实现保留前一摘要，并区分历史摘要和被切开的当前用户请求跨度前缀，必要时分别总结再合并。重复压缩时，只让当前有效的最新摘要参与投影，避免旧摘要重复进入输入。[S15][S20]

这些机制应建立在已经可测试的 Session Projection 上，不应在 V0 用一个“自动摘要历史”函数提前塞进循环。

### 7.3 Token 估算不是精确计量

实现可利用最近有效响应的 usage，再估算其后的消息；如果之后发生上下文编辑或压缩，旧 usage 不再必然代表当前视图，需重新估算。测试专门覆盖这类失效情况。[S15][S20]

源码还使用字符数除以 4 等估算策略。它是实现中的启发式，不是所有语言、模型和内容类型都适用的精确公式。Python 版本应记录估算方法与偏差，不把“预算内”当成无条件保证。

---

## 8. 运行控制与扩展：保留机制，不急着复制完整生态

### 8.1 Steering、Follow-up、Abort 分开研究

`steer()` 只是入队，在正常的回合边界被消费；所选版本不会因为新 steering 消息就跳过当前 assistant 已提出的工具批次。

`followUp()` 等待自然停止边界，没有待继续工具或 steering 时才处理。

`abort()` 发出取消信号。真正停止仍依赖 Provider、工具和相关回调响应信号，不意味着撤销已发生的副作用。[S06][S07][S08]

将三者都叫“中断恢复”会掩盖关键差异。建议 Python 分别命名并测试，尤其检查排队消息何时进入 history。

### 8.2 扩展不必侵入核心循环

扩展工具包装器主要适配执行上下文；工具拦截已经通过 AgentSession 安装到 Core 的 before/after hooks。上下文处理通过 transformContext 接入。[S09][S10][S16]

因此最初只需要实现几个明确、可测试的调用点，不必立刻复制动态发现、热加载、命令、UI 渲染等完整扩展系统。

### 8.3 不必把额外的运行环境管理类当成 V0 必修

AgentSessionRuntime 负责会话与 cwd 相关服务的替换，切换前取消并等待旧活动，发出关闭事件、释放旧 session，再创建并绑定新的 runtime。[S21]

这对完整交互产品有意义，但不是验证最小 Agent Loop 的前置条件。本项目先完成单个会话的核心行为即可。

---

## 9. 测试证据：哪些已经确认，哪些没有

### 9.1 阅读过的源码测试断言

| 测试区域 | 本轮看到的断言 | 对 Python 的启示 |
|---|---|---|
| Agent Loop | Provider 输入只含 transcript messages；transform 在 convert 之前 | 明确适配边界，验证传给模型的真实内容 |
| Agent Loop | 显式 continue 不与工具/队列造成的继续重复叠加 | 测试请求次数，而不只看最后文本 |
| Agent Loop | prepareRequest 后新增的 steering 不立即额外轮询 | 写清队列消费时机 |
| Agent | 异步监听器未完成时，prompt/idle 不得提前完成 | 将事件顺序纳入契约 |
| Agent | 运行中拒绝 reset、第二个 prompt 或 continue | 防止同一运行状态重入 |
| Session Projection | 编辑不改变原始历史；分支局部；重复压缩只留有效摘要 | 对投影采用无模型的确定性测试 |
| Context Estimate | 编辑/压缩后不能盲用旧 usage | 统计量也有有效性边界 |

这些是实际阅读到的测试代码，不代表本环境已经运行并通过对应 Vitest 套件。[S18][S19][S20]

### 9.2 实际运行的 5 个隔离验证

原始文件：

```text
packages/ai/src/utils/event-stream.ts
Git blob: eb968c016dfba785899c770099a74d0180b53364
```

本地复制文件通过 `git hash-object` 与仓库 blob 完全一致。使用 Node v22.16.0 的 TypeScript 类型剥离功能，直接导入该文件，未替换它的运行逻辑。

运行命令：

```bash
node --experimental-strip-types --test event-stream.test.mjs
```

结果：**5 tests，5 pass，0 fail**。

验证范围：

1. 事件 FIFO 顺序及最终结果可独立取得。
2. `end(result)` 能结束等待中的消费者。
3. 终止后的新事件被忽略，原最终结果不被覆盖。
4. error 事件形成带错误原因的最终消息，而不是把 result promise 变成 rejection。
5. 已有 duration 字段不被重新计时覆盖。

这只证明该小模块在这些输入下的行为，不证明 AgentLoop、Provider、Session 或整个 Pi 已通过运行验证。完整测试输出单独随报告提供。

---

## 10. Python 映射：架构对齐，不机械翻译

建议目标布局如下。只在阶段需要时建立文件，不要求第一天生成全部骨架。

```text
mini_agent/
├── ai/
│   ├── messages.py           # 统一消息与内容块
│   ├── events.py             # 模型响应事件
│   ├── provider.py           # 模型调用协议
│   └── providers/
│       └── openai.py         # 首个真实 Provider
├── agent/
│   ├── loop.py               # 模型与工具调度
│   ├── agent.py              # 状态包装、队列、取消
│   ├── events.py             # Agent 生命周期事件
│   └── tools.py              # AgentTool 契约与通用执行管线
├── harness/
│   ├── session.py            # AgentSession 组合与策略
│   ├── session_manager.py    # 条目树、leaf、JSONL
│   ├── projection.py         # 会话条目到模型上下文
│   ├── compaction.py         # 切分与摘要准备
│   └── tools/
│       └── read_file.py      # 具体文件工具
└── cli.py                    # 简单交互入口
```

`projection.py` 是 Python 端的文件拆分建议；Pi 当前将主要投影函数放在 session-manager.ts 中。职责保持一致，不必文件位置一致。

### 必须优先保留的语义

- Provider 适配、运行内核与 Harness 分层。
- 工具声明与可执行实现分离。
- 工具调用与结果的 ID 关联。
- 完成消息与流式中间态分离。
- 模型错误/截断、工具错误与正常结束分离。
- 关键事件处理完成后才能推进到依赖它的阶段。
- 原始会话记录与模型可见投影分离。
- 明确队列、取消与最终空闲的含义。

### 可以先简化的能力

- 只实现一个真实 Provider。
- 只支持文本和一个受限只读工具。
- 顺序执行工具，不立刻实现并行。
- Fake Stream 首先只发送必要事件，不立即实现全部 token delta。
- 只支持一条正常运行路径，不做自动重试、多模型切换、动态插件或完整 TUI。
- 先以内存实现会话树，再加入 JSONL。
- 引入预算限制作为学习项目的安全策略，不宣称这是对当前 Pi Core 公共接口的逐项复制。

### Python 技术取舍建议

使用 `dataclasses` 或少量校验模型表达数据，用 `Protocol` 表达 Provider 与工具接口，以 `async def` 和异步迭代表达 Pi 的执行/事件关系。

这里选择异步的理由不是“异步天然更好”，而是要保留被等待的回调、运行中排队与协作式取消等研究对象。第一版仍可只顺序执行一个工具。

TypeScript 的 declaration merging 不必照搬；Python 可采用显式联合类型或带 kind/role 的数据类型。错误传播、取消异常与未知事件的策略需要单独写入契约，不能假定 JavaScript Promise 与 Python Task 的行为完全相同。

---

## 11. 从源码依赖推导的学习路线

这是一条以 Pi 为唯一主架构基准的实现顺序，不是 Pi 作者公布的官方学习课程。

### R0：架构与契约研究

本报告已完成核心路径的源码与测试断言审读，并对 EventStream 做有限运行验证。后续对未实现机制按需继续深入，不以通读整个仓库作为开工条件。

**输出：** 角色边界、调用链、消息/状态流、源码索引与明确的范围裁剪。

### V0：Pi 风格的最小纵向闭环

实现最小消息类型、模型事件协议、Fake Provider、AgentLoop 和一个受限只读工具。保留基本生命周期事件、错误与截断判断；暂不实现持久化、压缩、完整流式文本和插件。

**验收：** 直接回答、多轮工具、同轮多个调用顺序执行、未知工具、参数错误、模型失败、截断调用不执行、预算停止。断言事件顺序、调用次数、结果 ID 与下一次模型输入。

**依据：** 消息和事件是 Core 的输入输出契约，不是后期界面附加项。[S06][S07][S18]

### V1：Stateful Agent 与运行控制

加入 Agent 状态包装，区分完成历史和 streaming 中间态；建立监听器等待、运行互斥、steering/follow-up 队列与协作式取消。

**验收：** 运行中禁止重入；排队不等于立即写入历史；每种队列在规定边界生效；监听器未完成不能报告 idle；取消不声称回滚工具副作用。

**依据：** Agent 类集中管理这些生命周期，而不是让调用方零散维护。[S08][S19]

### V2：Session Tree 与 Context Projection

先用内存实现条目树、当前叶节点、分支和投影，再加入 JSONL 保存/加载。先让投影在普通消息上工作，再加入 context edit 等选定机制。

**验收：** 不同分支生成不同上下文；投影可追溯来源；原始条目不因视图替换而改变；保存后按明确的 leaf 选择规则重建上下文。区分“已追加新条目的分支”和“只移动内存指针”的恢复预期。

**依据：** Compaction 和恢复处理都依赖投影，不应先写摘要后补存储模型。[S12][S13][S20]

### V3：AgentSession 组合

让 AgentSession 订阅核心事件，在完成消息边界追加条目，并在 prepareRequest 安装会话的当前投影。为单一正常路径区分低层 run 结束和上层 session 活动结束。

**验收：** 每个完成消息只持久化一次；下次请求看到已提交的视图；失败响应有记录；无需 CLI 也能跑完整会话。自动重试暂不作为完成条件。

**依据：** 会话策略由上层组合，而不是让底层循环直接依赖文件存储。[S09][S10][S11]

### V4：Compaction

先验证切分与投影重建，再接入真实摘要模型。把确定性的“选哪些条目、怎样接回去”和不确定的“摘要质量”分别评估。

**验收：** 不产生孤立工具结果；摘要失败不提交正常检查点；重复压缩不重复旧摘要；原始历史保留；压缩基于编辑后的视图。

**依据：** Pi 的 Compaction 是投影与持久化协作的检查点机制。[S12][S13][S15][S20]

### V5：按问题选择扩展

并行工具与双顺序语义、更多 Provider、工具 hooks、插件机制、分支摘要等按具体实验需要进入。MCP、完整 TUI、任意崩溃恢复不属于基础里程碑。

每项新增机制继续回答：它解决什么问题，Pi 的实际设计是什么，我们保留或裁剪什么，以及测试证据是什么。

### 对之前路线的实质调整

**消息/事件契约前移；Agent 状态和运行控制先于完整 Session Harness；会话树与投影先于 Compaction；其他仓库不再决定主架构。**

“架构一致”不等于“功能全量”。完成 V0–V3 就可以形成一个有明确 Pi 架构对应关系的研究成果，V4 再专门研究长上下文问题。

---

## 12. 尚未完成的验证与后续研究边界

本轮未运行仓库完整测试、真实 Provider 集成测试，未全面审阅 OAuth、全部 Provider 流转换、TUI/RPC、扩展加载与热重载、`pi-durable` 栈、全部自动恢复和分支摘要路径。

因此，本报告不作以下承诺：

- Pi 的全部实现没有缺陷。
- 所有事件监听器都被等待。
- 取消可以终止任何正在执行的底层操作。
- Session Tree 能回滚文件系统。
- JSONL 历史意味着任意崩溃点可以安全恢复。
- 字符启发式能精确控制所有模型的 Token。
- 当前源码结构是所有 Python 项目的唯一合理设计。

**能够确定的学习主线已经足够清楚：以 Pi 的数据契约和职责边界为骨架，先构建可观察、可测试的最小执行链，再逐步接入会话投影和策略。**

---

## 附录 A：源码证据索引

所有链接均固定到本报告 Commit，避免 main 更新导致笔记与源码错位。

| 编号 | 源码 | 本轮覆盖 |
|---|---|---|
| [S01] | 版本基线 | Commit 元数据；2026-10-22T07:02:01Z |
| [S02] | 模型、消息与流协议 | 重点阅读消息内容块、StopReason、SystemMessage、AssistantMessage、ToolResultMessage |
| [S03] | Transcript 重放与规范化 | normalizeContext、getCurrentSystemMessage、getCurrentTools、resolveTranscript、toToolDeclaration |
| [S04] | 模型事件流 | 完整文件；另执行 5 个针对该原始模块的隔离验证 |
| [S05] | OpenAI Responses 适配入口 | 读取 stream 的请求构建、流处理调用与终止检查；未审阅全部 Provider |
| [S06] | Agent Core 契约 | StreamFn、AgentLoopConfig、AgentState、AgentTool、AgentEvent |
| [S07] | Agent Loop 与工具管线 | 完整循环与执行管线：runLoop、streamAssistantResponse、prepareToolCall、runToolCall 等 |
| [S08] | Stateful Agent | 重点阅读 Agent 生命周期、队列、状态更新、监听器等待与异常路径 |
| [S09] | SDK 组合入口 | createAgentSession 的状态恢复、Agent 构造与 AgentSession 组合 |
| [S10] | AgentSession 事件、投影与边界 | 选读构造、工具 hooks、请求投影、下一轮刷新、事件持久化与恢复遗漏处理 |
| [S11] | AgentSession 上层执行生命周期 | _startRun、_prepareRun、_handlePostAgentRun、_runBeforeSettleBoundary、prompt |
| [S12] | 会话树与上下文投影 | buildContextEntries、buildSessionProjection、buildSessionContext |
| [S13] | 会话写入与分支 | 选读 _persist、_appendEntry、appendMessage、appendCompaction、getBranch、branch |
| [S14] | Harness 消息转换 | 完整文件；自定义消息与 convertToLlm |
| [S15] | Compaction | 选读估算、切分、投影准备、摘要与结果生成；并非全部恢复与分支摘要实现 |
| [S16] | 扩展工具适配 | 完整文件；包装器只适配上下文，拦截由 AgentSession/core hooks 完成 |
| [S17] | 具体只读工具 | ReadOperations 注入、参数、取消检查、输出截断与工具结果 |
| [S18] | Agent Loop 测试 | 选读 1–350、1220–1550：事件、转换顺序、prepareRequest、继续执行语义 |
| [S19] | Agent 生命周期测试 | 选读 300–540、650–950：异常事件、等待监听器、禁止重入、队列与取消 |
| [S20] | 上下文编辑测试 | 选读原始记录不变、最新编辑、分支局部性、重复压缩与 usage 失效 |
| [S21] | 运行环境替换 | 选读切换会话时 abort、关闭、dispose、重建 cwd 相关服务 |
| [S22] | 仓库范围与默认权限 | 其他包存在；默认使用启动进程权限，不内置通用文件/进程/网络隔离 |

## 附录 B：本项目建议维护的研究记录

```text
research/
├── pi-source-study.md         # 本报告
├── references.md              # 固定 Commit 与访问日期
├── invariants.md              # 已选定的行为不变量与测试映射
└── decisions/
    ├── 001-pi-layer-boundaries.md
    ├── 002-minimal-async-events.md
    ├── 003-sequential-tools-first.md
    └── 004-session-projection.md
```

这些文件是建议，不要求立即全部建立。最先记录的决策应是：采用哪些 Pi 语义、哪些能力暂不实现，以及怎样验证没有破坏保留的语义。

[S01]: https://github.com/earendil-works/pi/commit/7c22a93d0ed02092142fa63823e63d76aa4e2406
[S02]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/ai/src/types.ts#L300-L710
[S03]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/ai/src/utils/transcript.ts
[S04]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/ai/src/utils/event-stream.ts
[S05]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/ai/src/api/openai-responses.ts#L150-L260
[S06]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/src/types.ts
[S07]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/src/agent-loop.ts
[S08]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/src/agent.ts#L180-L650
[S09]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/sdk.ts#L210-L540
[S10]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/agent-session.ts#L480-L1410
[S11]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/agent-session.ts#L1860-L2240
[S12]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/session-manager.ts#L400-L610
[S13]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/session-manager.ts#L1000-L1660
[S14]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/messages.ts
[S15]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/compaction/compaction.ts
[S16]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/extensions/wrapper.ts
[S17]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/tools/read.ts
[S18]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/test/agent-loop.test.ts
[S19]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/agent/test/agent.test.ts
[S20]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/test/session-context-edit.test.ts#L1-L320
[S21]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/packages/coding-agent/src/core/agent-session-runtime.ts#L1-L360
[S22]: https://github.com/earendil-works/pi/blob/7c22a93d0ed02092142fa63823e63d76aa4e2406/README.md#L75-L110
