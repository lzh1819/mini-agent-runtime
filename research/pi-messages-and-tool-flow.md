# Pi 消息结构与一次工具调用：第一份本地源码阅读笔记

## 问题、基线与范围

这一步回答：用户输入、模型提出的工具调用和程序返回的工具结果，分别是什么数据？为什么不能都存成字符串？

- 阅读日期：2026-10-23。
- Pi Commit：`7c22a93d0ed02092142fa63823e63d76aa4e2406`。
- 前置概念：消息列表、对象字段、函数调用；本文对 TypeScript 类型使用处作直接解释。
- 重点选读模型类型、Agent 工具契约、AgentLoop 的请求和工具路径，以及 transcript 的规范化与工具声明转换。
- 选读工具调用、截断响应和多个调用的测试断言；本轮没有安装依赖、运行 Pi 测试或调用真实模型。

## 1. 消息角色与内容块是两层结构

`Message` 是四种消息类型的联合。这里的联合表示一条消息可以是其中任意一种，通过 `role` 区分：

| 类型与 role | 主要内容 | 作用 |
|---|---|---|
| `SystemMessage` / `system` | 指令、命名指令区块、工具声明增删 | 描述此时模型可见的指令和工具 |
| `UserMessage` / `user` | 字符串，或文本/图像块 | 提供用户输入 |
| `AssistantMessage` / `assistant` | 文本、thinking、工具调用块；另有模型与终止元数据 | 保存模型响应 |
| `ToolResultMessage` / `toolResult` | 调用 ID、工具名、文本/图像块、错误标记 | 保存执行管线整理后的工具结果 |

`AssistantMessage.content` 是数组，同一响应可以同时包含解释文字与多个工具调用。`ToolCall` 属于数组中的内容块，不是第五种消息角色：

```text
AssistantMessage
  role = assistant
  content
    ├── TextContent(type=text, text=我先查看文件)
    └── ToolCall(type=toolCall, id=call-1, name=read_file, arguments=...)
```

因此，一个字符串无法直接保留“谁说的、这是文本还是调用、调用参数是什么、结果属于哪次调用”等结构关系。

证据：[模型类型](../references/pi/packages/ai/src/types.ts#L422)，其中 ToolCall 在 444 行，四种消息及联合在 556–654 行。

## 2. 工具声明、调用、实现与结果各有职责

| 对象 | 关键内容 | 负责方 |
|---|---|---|
| `Tool` | `name`、`description`、参数 Schema | 应用向模型声明可用能力 |
| `ToolCall` | `id`、`name`、JSON 参数 | 模型响应经 Provider 适配后提出的调用 |
| `AgentTool` | 工具声明加 `execute()` 等执行能力 | Runtime 持有的本地实现 |
| `ToolResultMessage` | `toolCallId`、`toolName`、`content`、`isError` | Runtime 将执行结果整理成消息 |

模型提出工具名和参数，实际函数由 Runtime 查找并调用。`toToolDeclaration()` 只复制声明字段，不把可执行函数带入 transcript。

`AgentToolResult` 也不等于 `ToolResultMessage`：工具返回 `content`、`details` 等结果，执行管线再添加调用关联、错误状态和时间等字段。`content` 面向模型，`details` 用于日志或界面，`structuredContent` 用于程序调用者；不能将内部元数据默认全部当成模型输入。

证据：[Tool 声明](../references/pi/packages/ai/src/types.ts#L764)、[AgentToolResult 与 AgentTool](../references/pi/packages/agent/src/types.ts#L424)、[声明转换](../references/pi/packages/ai/src/utils/transcript.ts#L123)、[结果消息构造](../references/pi/packages/agent/src/agent-loop.ts#L940)。

## 3. 用一个例子追踪数据

下面是阅读辅助示意，省略了 system 工具声明和 timestamp、api、provider、model、usage 等字段，不是完整 Pi 消息对象，也不是本轮实际运行输出：

```json
[
  {"role": "user", "content": "解释 example.py"},
  {
    "role": "assistant",
    "content": [
      {"type": "toolCall", "id": "call-1", "name": "read_file", "arguments": {"path": "example.py"}}
    ],
    "stopReason": "toolUse"
  },
  {
    "role": "toolResult",
    "toolCallId": "call-1",
    "toolName": "read_file",
    "content": [{"type": "text", "text": "print('hello')"}],
    "isError": false
  },
  {
    "role": "assistant",
    "content": [{"type": "text", "text": "这个文件会输出 hello。"}],
    "stopReason": "stop"
  }
]
```

普通路径中，在没有排队消息、自定义继续/结束决策或工具提前终止时：

1. `runAgentLoop()` 把输入加入上下文，并将可执行工具的声明变化写入 system 消息。
2. `streamAssistantResponse()` 依次执行 context transform、`convertToLlm()`、`normalizeContext()`，再调用模型流函数。
3. 获得最终助手响应后，从 `content` 中筛出 `type === "toolCall"` 的块。
4. 工具管线按名称查找工具、预处理与校验参数、处理 hooks/取消，再调用本地 `execute()`。
5. 构造结果消息，将 `ToolCall.id` 写入 `ToolResultMessage.toolCallId`，把结果追加到上下文。
6. 再次请求模型，模型现在能够看到前面的调用与对应结果，并给出最终回答。

这一示例通常需要两次模型请求。工具执行在两次请求之间发生；不能把第一次模型响应里的调用描述当作工具已经执行。

证据：[输入与循环入口](../references/pi/packages/agent/src/agent-loop.ts#L111)、[工具提取与结果追加](../references/pi/packages/agent/src/agent-loop.ts#L250)、[模型请求](../references/pi/packages/agent/src/agent-loop.ts#L390)、[工具准备](../references/pi/packages/agent/src/agent-loop.ts#L718)、[实际 execute 调用](../references/pi/packages/agent/src/agent-loop.ts#L831)。

## 4. 为什么结果要关联调用 ID

工具名只回答“调用什么工具”；调用 ID 回答“这是哪一次调用的结果”。同一响应可以两次调用同一个工具、读取两个文件，此时单靠 `read_file` 无法区分结果。

实现中 `createToolResultMessage()` 明确使用 `finalized.toolCall.id` 填充 `toolCallId`。选读的并行测试提出 `tool-1`、`tool-2` 两次 echo 调用，并断言完成事件可为 `tool-2`、`tool-1`，最终结果消息则按 `tool-1`、`tool-2` 组织。调用 ID 与记录顺序是两个不同概念；本课仅用这组断言说明关联信息的必要性，不展开并行调度。

证据：[结果 ID 赋值](../references/pi/packages/agent/src/agent-loop.ts#L940)、[多个调用的测试与断言](../references/pi/packages/agent/test/agent-loop.test.ts#L709)。后者已阅读，未运行。

## 5. 影响消息设计的两个边界

### 完成响应与流式中间态

Provider 的事件包括 `start`、各种增量、`done` 和 `error`。AgentLoop 可将 partial 放入当前上下文以更新流式状态，但在工具分派前，会等待 `streamAssistantResponse()` 返回最终消息。`toolcall_delta` 或 `toolcall_end` 本身不触发此处的工具执行。

这说明消息结构需要表达完整响应和终止状态，不能把一段流式文本默认当作最终答案。本课不展开完整事件系统。

### 截断、失败与正常完成

`AssistantMessage.stopReason` 区分 `stop`、`toolUse`、`length`、`error`、`aborted` 等状态。在选读路径中：

- `error` / `aborted` 进入结束分支，不分派其中的工具调用。
- `length` 且含工具调用时，所有调用都转成关联的错误结果，不执行工具；循环仍可继续请求模型。
- 其余情况实际由 `content` 中是否存在工具调用决定是否进入工具管线，不能将实现概括成只检查 `stopReason === "toolUse"`。

截断保护的理由写在源码注释中：被截断的参数即使能够解析、通过 Schema 校验，也可能丢失一部分含义。选读测试断言工具执行列表为空、错误标记为真、模型请求次数为两次。这是阅读到的预期行为，本轮没有执行验证。

证据：[最终消息替换](../references/pi/packages/agent/src/agent-loop.ts#L425)、[停止原因与分派](../references/pi/packages/agent/src/agent-loop.ts#L254)、[截断错误结果](../references/pi/packages/agent/src/agent-loop.ts#L488)、[截断测试](../references/pi/packages/agent/test/agent-loop.test.ts#L456)。

## 6. 请求上下文的边界

在此版本中，指令和工具声明由 transcript 的 system 消息承载，`toolsAdded` / `toolsRemoved` 表达变化。AgentLoop 同步可执行工具集合与声明，`toToolDeclaration()` 去掉执行字段；`normalizeContext()` 将兼容入口的顶层 prompt/tools 折入 system 消息。

`AgentMessage` 允许应用扩展自定义消息；`convertToLlm()` 把它们转换或过滤为模型层的 `Message[]`。因此“Agent 内部记录”与“交给 Provider 的模型消息”之间有转换边界。本轮没有阅读真实供应商的请求序列化，不把 Pi 的 `toolResult` 角色等同于所有供应商的 HTTP 格式。

证据：[AgentMessage 与转换契约](../references/pi/packages/agent/src/types.ts#L197)、[工具声明同步](../references/pi/packages/agent/src/agent-loop.ts#L332)、[transcript 规范化](../references/pi/packages/ai/src/utils/transcript.ts#L10)。

## 7. 对后续 Python 实现的建议与未决问题

以下是根据本次阅读提出的建议，尚未形成 Python 接口或已运行的行为：

- 优先显式表达消息角色、文本块、工具调用、工具结果和停止原因。
- 保留调用 ID、结构化参数和错误标记，以便测试下一次模型输入是否正确。
- 工具声明与本地可执行实现分开，避免把函数放入模型输入。
- 图片、thinking、供应商签名、并行调度和动态 system 更新可以分阶段研究；裁剪时记录边界。

下一教学步骤建议先设计并验证最小消息数据契约：用一个静态工具调用示例检查角色、内容类型、调用与结果关联。它承接本次源码认识，并为 Fake Provider 的输入输出提供明确约束；具体字段、校验策略和 Python 类型表达方式需要在实施前说明。

可用于讨论的问题：同一响应两次调用 `read_file` 时，怎样确定两个结果各属于哪一次调用？若工具返回的文本描述失败，数据中还应怎样标记？

## 8. L006：Pi 字段与 V0 Python 消息契约对照

### 对照规则与本步边界

本节于 2026-10-23 逐项核对同一 Pi Commit 的原始类型，作为实现前的推荐基线，尚未写入 Python 模块。用户本步要求先做字段对照，不等同于确认了全部设计取舍。

- **保留**：保留字段职责；camelCase 转为 snake_case 是 Python 命名选择。
- **收窄**：只支持字段类型的明确子集，不能称为完整 Pi 协议兼容。
- **暂缓**：不在 V0 消息类型中实现，说明缺失能力及后续接入条件。
- **Python 自选**：`dataclass`、`Literal`、标签不可作为构造参数等属于实现方式，不是 Pi 的原生 Python 设计。

相较对话中的首版草案，本节建议补回时间戳、助手来源字段和参数错误信息。前两项保留基础可追溯性，不需要引入新的执行机制；参数错误则直接影响执行安全。它们是有依据的修订，不是为了补齐 Pi 的所有字段。

### 共同字段与 JSON 值

| Pi 字段/类型 | Python 建议 | V0 处理与理由 |
|---|---|---|
| 四种消息的 `role` | `role` | 保留 `system`、`user`、`assistant`、`toolResult` 的原有字符串，避免混淆消息与内容块 |
| 四种消息的 `timestamp` | `timestamp: int` | 保留 Unix 毫秒，沿用原单位；不把字段名改成含义不同的秒数 |
| `JsonValue` / `JsonObject` | 递归 JSON 值 / `dict[str, JsonValue]` | 保留结构化 JSON 语义，不用 `Any` 取代参数约束；Python 类型标注并不自动执行 JSON 校验 |

时间戳由创建消息的层明确提供：助手消息表示请求开始时间，工具结果表示结果创建时间，其他消息沿用记录时间的毫秒含义。Fake 场景使用明确的模拟值，后续真实路径提供实际时间；不在每个数据类中隐式读取时钟，也不以默认 `0` 冒充真实时间。

证据：[四种消息](../references/pi/packages/ai/src/types.ts#L556)、[JSON 类型](../references/pi/packages/ai/src/types.ts#L486)。

### TextContent 与暂缓的内容块

| Pi 字段 | Python 建议 | V0 处理与理由 |
|---|---|---|
| `TextContent.type` | `type = "text"` | 保留内容类型标签 |
| `TextContent.text` | `text: str` | 保留文本内容 |
| `TextContent.textSignature` | 暂无 | 暂缓供应商特有的文本重放信息；不能据此承诺真实 Provider 的完整重放兼容 |
| `ThinkingContent` 的 `type`、`thinking`、`thinkingSignature`、`redacted` | 暂无对应内容类型 | 暂缓 thinking 与加密重放；不将其假装成普通文本 |
| `ImageContent` 的 `type`、`data`、`mimeType` | 暂无对应内容类型 | 暂缓多模态，不静默丢弃图像后声称保留了原输入 |

证据：[内容块](../references/pi/packages/ai/src/types.ts#L422)。接入真实 Provider 前必须核对其重放需求；需要被省略内容或签名的路径不在 V0 支持范围内。

### ToolCall

| Pi 字段 | Python 建议 | V0 处理与理由 |
|---|---|---|
| `type` | `type = "toolCall"` | 保留标签；仍是助手内容块，不是消息角色 |
| `id` | `id: str` | 保留单次调用身份，不用工具名代替 |
| `name` | `name: str` | 保留工具查找名称 |
| `arguments` | `arguments: JsonObject` | 保留已解析的参数对象，顶层不是任意 JSON 值 |
| `argumentsError` | `arguments_error: str | None` | 保留参数解析失败原因；可携带原参数文本，供后续校验明确拒绝执行 |
| `thoughtSignature` | 暂无 | 暂缓 Google 等供应商特有的重放语义 |
| `namespace` | 暂无 | 暂缓命名空间和动态工具加载，当前仅支持普通工具名 |

Pi 在完整参数不是合法 JSON 对象时可保存 `arguments = {}` 与 `argumentsError`。两者共同表达失败；不能只保留 `{}`，把解析错误变成一个看似有效的空参数调用。参数 Schema 校验与 JSON 解析错误是不同层面的检查。

证据：[ToolCall](../references/pi/packages/ai/src/types.ts#L444)、[先检查 argumentsError 的校验入口](../references/pi/packages/ai/src/utils/validation.ts#L317)。本步核对了源码，没有执行这个校验函数；Python 校验尚未实现。

### SystemMessage 与 UserMessage

| Pi 字段 | Python 建议 | V0 处理与理由 |
|---|---|---|
| `SystemMessage.role` | `role = "system"` | 保留 |
| `SystemMessage.content` | `content: str` | 从字符串或文本块数组收窄为单一字符串形式 |
| `SystemMessage.toolsAdded` | `tools_added: list[ToolDeclaration]` | 保留初始工具声明；工具含义仍来自 transcript，不另建无记录的模型可见工具集合 |
| `SystemMessage.toolsRemoved` | 暂无 | 暂缓运行中工具移除和 `ToolReference` |
| `SystemMessage.sections` | 暂无 | 暂缓命名指令区块的替换与删除 |
| `SystemMessage.timestamp` | `timestamp: int` | 保留，单位见共同字段 |
| `UserMessage.role` | `role = "user"` | 保留 |
| `UserMessage.content` | `content: str` | 从字符串或文本/图像块数组收窄为文本字符串 |
| `UserMessage.timestamp` | `timestamp: int` | 保留，单位见共同字段 |

V0 建议仅支持开头的 system 状态：`tools_added` 可为空，表示没有初始工具；不将后续 system 消息偷偷折叠成新状态。未来请求边界应明确拒绝暂不支持的中途更新，而不是宣称已实现 Pi 的完整 transcript 重放。数据类本身不会自动检查消息在列表中的位置。

证据：[SystemMessage 与 UserMessage](../references/pi/packages/ai/src/types.ts#L546)。

### AssistantMessage

| Pi 字段 | Python 建议 | V0 处理与理由 |
|---|---|---|
| `role` | `role = "assistant"` | 保留 |
| `content` | `list[TextContent | ToolCall]` | 保留块数组与多调用能力，收窄掉 thinking |
| `api` | `api: str` | 保留协议来源，不在本步建立 API 注册表 |
| `provider` | `provider: str` | 保留供应商来源；Fake 来源明确标成模拟来源 |
| `model` | `model: str` | 保留所记录的模型标识，方便追踪响应来源 |
| `responseModel` | 暂无 | 暂缓请求模型与实际响应模型的额外区分 |
| `responseId` | 暂无 | 暂缓供应商响应追踪标识，不承诺基于它继续请求 |
| `providerThinkingLevel` | 暂无 | 暂缓供应商 thinking 配置记录 |
| `thinkingLevel` | 暂无 | 暂缓 Agent 请求的 thinking 配置记录 |
| `diagnostics` | 暂无 | 暂缓专用诊断结构；保留基本错误原因，不假装完整诊断 |
| `usage` | 暂无 | 明确裁剪 Pi 原本必填的用量结构；V0 不承诺 Token/费用统计，不填虚构零值冒充实际用量 |
| `stopReason` | `stop_reason: FinalStopReason` | 保留五种受支持的最终状态，完整对照见下表 |
| `deferred` | 暂无 | 暂缓异步任务句柄及轮询恢复 |
| `errorMessage` | `error_message: str | None` | 保留模型失败原因 |
| `rawStopReason` | 暂无 | 暂缓供应商原始终止字符串，保留 V0 统一终止状态 |
| `endTurn` | 暂无 | 暂缓供应商调试提示；Pi 注释明确它当前不影响 Agent 控制流 |
| `timestamp` | `timestamp: int` | 保留请求开始时间，Unix 毫秒 |
| `durationMs` | 暂无 | 暂缓响应时长统计 |

`api/provider/model` 是 Pi 原本要求的字段，建议明确填写而不是按需猜测。保留字段并不等于已经具备 Provider 适配；相反，省略 `usage` 和重放相关能力意味着 V0 消息不能作为完整 Pi 消息的无损替代品。

证据：[AssistantMessage](../references/pi/packages/ai/src/types.ts#L580)。`Usage` 内部的 Token、缓存与费用明细作为一个完整的暂缓专题，不在本步展开成 Python 字段。

### StopReason 的范围

| Pi 取值 | Python V0 建议 | 含义与边界 |
|---|---|---|
| `stop` | 保留 | 模型正常结束本次响应；不单凭此标签断言整个用户任务结束 |
| `toolUse` | 保留 | 工具调用相关终止状态；执行循环仍需检查实际内容块 |
| `length` | 保留 | 输出截断；未来执行循环必须阻止执行其中的工具调用 |
| `error` | 保留 | 模型失败，保留原因 |
| `aborted` | 保留 | 模型取消状态，不表示回滚外部副作用 |
| `pending` | 不属于 V0 最终消息类型 | 未来流式中间态单独设计，不把 pending 消息交给工具分派 |
| `deferred` | 暂不支持 | 无任务句柄、轮询和恢复机制时，不能转换成正常 `stop` |

建议将五种状态的 Python 类型命名为 `FinalStopReason`，明确其为 Pi `StopReason` 的受支持最终状态子集。这是对首版草案名称的修正，不是重定义 Pi 的原始七种取值。未知或暂不支持的状态应在未来适配边界明确报错，不静默替换成成功状态。

证据：[StopReason](../references/pi/packages/ai/src/types.ts#L484)、[AgentLoop 的实际分派](../references/pi/packages/agent/src/agent-loop.ts#L254)。本步不实现任何状态处理行为。

### ToolResultMessage

| Pi 字段 | Python 建议 | V0 处理与理由 |
|---|---|---|
| `role` | `role = "toolResult"` | 保留 Pi 内部角色值 |
| `toolCallId` | `tool_call_id: str` | 保留与原调用的关联 |
| `toolName` | `tool_name: str` | 保留工具身份与可读性 |
| `content` | `list[TextContent]` | 收窄为文本块结果，暂缓图像 |
| `details` | 暂无 | 暂缓日志/界面专用元数据；不将它混入模型可见文本 |
| `usage` | 暂无 | 暂缓工具自身的用量统计 |
| `nestedCalls` | 暂无 | 暂缓工具内部再次调用工具的记录与相关类型 |
| `isError` | `is_error: bool` | 保留且要求明确填写，避免只在文本中写 Error |
| `timestamp` | `timestamp: int` | 保留结果创建时间，Unix 毫秒 |
| `durationMs` | 暂无 | 暂缓执行时长统计 |

这里对照的是执行管线产生的消息，不是 `AgentToolResult`。后者的 `structuredContent`、`terminate` 等属于工具执行契约，不能误写成此消息类型原本拥有的字段；它们的 Python 设计留给工具专题。

证据：[ToolResultMessage](../references/pi/packages/ai/src/types.ts#L635)、[AgentToolResult](../references/pi/packages/agent/src/types.ts#L424)。

### Tool 声明与类型表达差异

| Pi 字段/类型 | Python 建议 | V0 处理与理由 |
|---|---|---|
| `Tool.name` | `ToolDeclaration.name: str` | 保留；类型改名用于突出“声明” |
| `Tool.description` | `ToolDeclaration.description: str` | 保留工具用途说明 |
| `Tool.parameters` | `ToolDeclaration.parameters: JsonObject` | 保留 JSON Schema 语义，收窄到 JSON 对象表示；Pi 使用 TypeBox `TSchema`，不承诺 Python 复刻其类型推导或校验器 |
| `Tool.constrainedSampling` | 暂无 | 暂缓约束采样配置及 grammar 等相关类型 |
| `Message` 联合 | 四种消息的 Python 联合类型 | 保留四种角色关系；不新增 `ToolCallMessage` |

证据：[Tool](../references/pi/packages/ai/src/types.ts#L764)、[Message](../references/pi/packages/ai/src/types.ts#L654)。

### 后续实现的检查点

本表是可追溯的设计对照，尚未验证 Python 行为。落实时至少需要检查：调用 ID 与结果关联、参数错误原因的保留、终止状态不被误标为成功、时间戳单位一致，以及暂不支持的内容不会被悄悄丢弃。

`dataclass` 和类型标注负责表达形状，不自动完成信任边界校验；单条消息也不能独自验证整段 transcript 的关联和位置。下一步应先说明最小消息模块及静态示例的具体范围，再实施；Provider 解码、Schema 校验和执行安全分别在对应阶段加入测试。
