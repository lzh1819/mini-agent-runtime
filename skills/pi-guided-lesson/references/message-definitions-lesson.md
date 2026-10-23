# 课堂样例：一次工具交互中的最小消息定义

备课说明：本例依据用户对旧样例知识前提与课程粒度的反馈重写，尚未获得逐项审阅认可。前置内容是上一课的字符串调用，以及 Python 字符串、字典、列表和函数。源码基于 Pi Commit `7c22a93d0ed02092142fa63823e63d76aa4e2406`。本例包含拟议类型和手工构造的交互记录，属于教学材料，不表示仓库已经实现该契约或运行过模型、工具。原样例保留在 Git 提交 `29e3be4` 中。本段是备课信息，不作为课堂开场。

---

**一、怎样记录一次需要读取文件的问答**

上一课可以把一句话交给函数，再取得一个回答：

```python
generate("比较 a.txt 和 b.txt 的内容。")
```

这句话只有文件名，没有文件内容。为了比较它们，程序需要读取文件，再把结果交给模型。我们把程序能执行的具体操作称为工具，例如 `read_file`。

一次交互就可能经历：

```text
用户提出比较请求
模型提出读取两个文件的请求
程序读取文件并返回结果
模型根据结果回答
```

后面的回答依赖前面的请求和读取结果，因此需要把这些交互保存下来，作为后续调用的输入。

先用列表保存文字，可以保留内容与顺序。但一条“读取失败”的文字来自用户、模型还是读取程序，含义并不相同。程序要清楚表达来源，就需要在保存内容时同时记录身份。

我们用 `role` 表示身份，`content` 保存内容：用户提出的问题记为 `user`，模型产生的回答记为 `assistant`。

这个应用还要求模型“依据文件内容回答，读取失败时如实说明”。这是应用预先给出的工作要求，和用户本次要比较哪两个文件来源不同。把它单独记录为 `system`，可以保留这项区别。

现在看模型的读取请求。如果模型只返回一句“请读取 a.txt”，程序必须从自由文本里猜出操作与参数。要让程序明确知道执行什么，可以把请求写成：

```python
{"name": "read_file", "arguments": {"path": "a.txt"}}
```

`name` 指出操作，`arguments` 保存执行所需的参数。程序持有 `read_file` 的实际实现，模型只提出请求。这个结构化请求称为工具调用。

模型也可能同时说“我先读取这两个文件”，再给出两个读取请求。因此，一次助手输出需要容纳不同种类的内容。可以将它表示为内容列表，并用 `type` 区分普通文字与工具调用：

```text
助手消息
  文字：我先读取这两个文件。
  调用：read_file，path=a.txt
  调用：read_file，path=b.txt
```

这里有两个层次：外层角色说明内容由谁产生，内层类型说明这项内容是什么。工具调用来自模型，所以它仍然位于助手消息中。

读取结束后，结果由程序产生，需要作为工具结果消息交回。两次调用的工具名都是 `read_file`，只记录工具名无法区分结果属于哪次请求。给每次调用一个标识，并让结果带回这个标识，就可以建立对应关系：

```text
call-a：read_file(path=a.txt) → 结果带回 call-a
call-b：read_file(path=b.txt) → 结果带回 call-b
```

如果 a.txt 读取成功，b.txt 不存在，还需要区分正常文件内容与错误说明。用一个明确的成功/失败标记表达状态，结果文本负责保存文件内容或错误原因，调用方就不必从自然语言猜测是否成功。

这一次交互需要的消息关系已经完整：应用给出指令，用户提出问题，助手产生文字与调用，工具结果关联调用，助手最后依据结果回答。

**二、Pi 的对应结构与 Python 定义**

Pi 用以下结构表达这些职责：

| 已经遇到的职责 | Pi 对应实现 |
|---|---|
| 应用提供指令 | [SystemMessage](../../../references/pi/packages/ai/src/types.ts#L556) |
| 用户提出问题 | [UserMessage](../../../references/pi/packages/ai/src/types.ts#L574) |
| 模型产生文字或工具调用 | [AssistantMessage](../../../references/pi/packages/ai/src/types.ts#L580) 的内容列表 |
| 区分文字和结构化调用 | [TextContent](../../../references/pi/packages/ai/src/types.ts#L422)、[ToolCall](../../../references/pi/packages/ai/src/types.ts#L444) 的 `type` 字段 |
| 程序交回结果、关联请求并表达失败 | [ToolResultMessage](../../../references/pi/packages/ai/src/types.ts#L635) 的 `toolCallId` 与 `isError` |

Pi 的 [工具处理路径](../../../references/pi/packages/agent/src/agent-loop.ts#L268) 从助手内容中取出工具调用；[结果构造函数](../../../references/pi/packages/agent/src/agent-loop.ts#L940) 将调用的 `id` 放入结果的 `toolCallId`。这两个使用位置说明，调用与结果的关联确实参与程序执行。

消息列表中既有用户问题，也有助手调用和程序返回值。`ToolCall` 是助手内容的一种，并不是一个独立的发言角色。

Python 可以用 `TypedDict` 描述这些字典。`Literal` 表达固定的角色或内容类型，`A | B` 表示该位置可以放 A 或 B。下面的定义完整覆盖这个文件读取例子：

```python
from typing import Literal, TypedDict


class TextContent(TypedDict):
    type: Literal["text"]
    text: str


class ToolCall(TypedDict):
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: dict[str, str]


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: list[TextContent | ToolCall]


class ToolResultMessage(TypedDict):
    role: Literal["toolResult"]
    tool_call_id: str
    content: str
    is_error: bool


Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
```

这几类数据分开定义，是因为它们需要的信息不同。例如助手消息可以包含调用，工具结果需要关联调用；全部塞入同一个带许多可选字段的字典，会让这些关系变得不明确。

读取工具目前只接收字符串路径，所以这个例子将参数写为 `dict[str, str]`。应用指令、用户输入和读取结果都是文本，直接使用字符串；助手需要在同一条消息中同时表达文字与调用，因此使用带类型标签的内容列表。

Pi 的工具结果内容使用内容块列表；这里的读取结果只有文本，选择字符串即可表达。`tool_call_id` 与 `is_error` 对应 Pi 的 `toolCallId` 与 `isError`，改名采用 Python 的常见风格。

将这些类型组合起来，可以写出一段完整记录。下面的文件内容与错误由示例手工给定：

```python
history: list[Message] = [
    {
        "role": "system",
        "content": "依据文件内容回答，读取失败时如实说明。",
    },
    {"role": "user", "content": "比较 a.txt 和 b.txt 的内容。"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我先读取这两个文件。"},
            {
                "type": "toolCall", "id": "call-a", "name": "read_file",
                "arguments": {"path": "a.txt"},
            },
            {
                "type": "toolCall", "id": "call-b", "name": "read_file",
                "arguments": {"path": "b.txt"},
            },
        ],
    },
    {
        "role": "toolResult", "tool_call_id": "call-a",
        "content": "苹果", "is_error": False,
    },
    {
        "role": "toolResult", "tool_call_id": "call-b",
        "content": "文件不存在：b.txt", "is_error": True,
    },
    {
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": "a.txt 内容是苹果；b.txt 读取失败，无法完成比较。",
        }],
    },
]
```

从这份记录中，可以直接定位用户请求、模型提出的两次调用、每次调用对应的结果，以及依据结果产生的回答。

`TypedDict` 只描述形状，不会自动校验外部数据。读取模型响应时，需要检查实际字段；执行调用前，需要确认工具存在、参数合法、调用标识可关联。消息定义让这些检查有明确对象，但定义本身不会执行工具。

本课的代码重点是 `messages.py` 中的完整类型，以及 `tests/test_messages.py` 中能够组合这些类型的交互样例。验证应检查四种消息角色、助手内容的两种形状、两次同名调用各自的结果关联，以及成功和失败结果；不能只断言列表长度或字段存在。

现有 Fake 只接受用户和助手文本，不能直接接收这份新记录。实施时应明确其输入契约：保留文本 Fake 时，把它的参数标为纯文本消息列表；工具交互样例用于独立验证新消息定义。无需为了演示类型，把整套工具调度写进 Fake。相关代码、测试、课程笔记和 README 在同一步同步。

这节课完成的是一份能表达整个交互过程的消息契约。随后可以围绕已经出现的 `ToolCall`，研究程序怎样找到实际工具、校验参数、执行并构造对应结果。
