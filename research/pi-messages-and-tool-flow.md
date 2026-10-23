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
