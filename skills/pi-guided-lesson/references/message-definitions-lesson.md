# 课堂样例：从项目问答到最小消息定义

备课说明：本例按用户对知识前提、课程粒度和推导连贯性的反馈修订，尚未获得逐项审阅认可。前置知识是上一课的字符串调用，以及 Python 字符串、字典、列表和函数。Pi 源码固定为 `7c22a93d0ed02092142fa63823e63d76aa4e2406`。代码与文件内容为教学示例，不表示仓库已实现或模型已执行；本段不作为课堂开场。

---

**一、一次项目问答逐渐需要记录什么**

上一课的函数接收一段文本，返回一段文本：

```python
generate("项目说明：这是一个用于学习 Agent 的 Python 项目。请介绍它。")
```

回答所需的项目说明就在输入中。假设回答之后，用户接着说“再简短一些”。如果下一次只传入这几个字，前面的说明和回答都丢了。

把之前的交互按顺序保存，下次一起传入，就能保留这些信息。但只有一串文字，无法明确识别哪些来自用户、哪些是助手回答。可以先约定双方严格交替；用户在回答前连续补充“还要说明学习用途”时，这个位置约定就不成立了。

因此，每条记录需要同时保存来源和内容。用 `role` 表达来源、`content` 保存内容，可以得到：

```python
dialogue = [
    {"role": "user", "content": "请根据这段项目说明介绍项目：……"},
    {"role": "assistant", "content": "这是一个用于学习 Agent 的 Python 项目。"},
    {"role": "user", "content": "再简短一些。"},
]
```

列表保留先后关系，每条记录自己说明身份，不再依赖发言必须交替。

这个项目问答应用还约定：回答必须说明依据，无法确认时如实说明。用户可以不断更换问题，这项要求却一直有效。最直接的做法，是把它作为应用配置，和本次对话一起交给模型。此时需要区分两项输入：应用持续提供的要求，以及用户当前提出的任务；不必把要求伪装成用户曾经说过的话。

接着，用户不再粘贴说明，改为“读取 README.md，介绍这个项目”。程序可以先读取这个确定的文件，再把内容放入输入。这个办法足以完成当前任务，模型无需提出读取请求。

用户进一步要求：“检查 README.md 与它提到的实现是否一致。”README 写着 a.py 负责读取、b.py 负责写入。要检查一致性，还得查看相应实现。

程序可以继续写固定步骤：读 README，再读 a.py 和 b.py。但换一个项目，说明中提到的文件和需要检查的内容都会变化。如果希望模型根据已经读到的说明决定下一步查看什么，程序就需要接收模型提出的操作请求，再执行并交回结果。

双方先约定程序提供 `read_file(path)`：它接收一个路径字符串，返回文件内容。模型产生“读取哪个路径”的请求，实际打开文件的动作仍由程序完成。这样的可执行操作称为工具。

请求如果只是“请帮我读一下 a.py”，程序还要从句子中识别操作和参数。把它们分开记录，就可以明确交接：

```python
{"name": "read_file", "arguments": {"path": "a.py"}}
```

工具执行后，结果也要进入下一次输入。否则模型虽然提出了读取请求，仍然看不到读取内容。此时一段交互包含：

```text
用户提出检查任务
模型请求读取 README
程序交回 README 内容
模型根据内容请求读取 a.py 和 b.py
程序交回读取结果
模型根据结果作答
```

这里新增了来自执行程序的信息。记录时需要把它和用户输入、模型输出区别开，并保留它回答的是哪个读取请求。

再看模型的同一次输出：它可能先说“我再检查这两个实现”，接着提出两个读取请求。现在需要区分面向用户的文字与交给程序执行的请求，并能在一份助手输出中容纳它们。把文字和调用分别存入两个字段可以做到；把它们组织成带类别的内容序列，也可以做到。前一种方式分开保存两类数据，后一种方式还能直接表达它们在同一输出中的排列。

两个请求都调用 `read_file`，所以工具名不足以说明一条结果属于哪次调用。固定按位置对应需要始终遵守同一顺序约定；若结果本身带有请求标识，就可以直接查到归属。这是在保存内容之外，还需要保留的一项关联信息。

如果 a.py 读取成功，b.py 不存在，程序还需要准确表达一项成功、一项失败。只给出“文件不存在”这几个字，无法区分它是错误说明，还是文件恰好包含的正文。因此，工具结果需要分别保存执行状态与返回文本。

**二、这些信息怎样对应到 Pi 和 Python**

最先需要表达的是用户输入的来源和内容。Pi 的 [UserMessage](../../../references/pi/packages/ai/src/types.ts#L574) 用 `role: "user"` 标记来源，`content` 保存内容。Python 可以用 `TypedDict` 描述相应字典，`Literal` 表达固定的角色值：

```python
from typing import Literal, TypedDict


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str
```

`TypedDict` 是字段形状的声明，运行时的对象仍然是字典。

应用持续提供的要求，可以像前面所说的那样单独配置。Pi 选择将它放进 [SystemMessage](../../../references/pi/packages/ai/src/types.ts#L556)，在消息序列中明确标为应用指令；[请求上下文规范化](../../../references/pi/packages/ai/src/utils/transcript.ts#L30) 也会把入口提供的 `systemPrompt` 整理成这种消息。

因此，区分应用要求与用户任务是需要表达的信息，把它们统一组织为不同角色的消息是 Pi 的表示选择。我们采用这一组织方式：

```python
class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str
```

接下来是助手输出。前面已经遇到“文字加两个读取请求”，Pi 的 [AssistantMessage](../../../references/pi/packages/ai/src/types.ts#L580) 采用有序内容块列表。每块用 `type` 表明类别：普通文字是 [TextContent](../../../references/pi/packages/ai/src/types.ts#L422)，操作请求是 [ToolCall](../../../references/pi/packages/ai/src/types.ts#L444)。这正好能够表达混合内容及其先后关系，我们也选择这种表示。

工具调用中的 `name`、`arguments` 对应操作和参数。`id` 是请求标识，稍后结果会引用它；当前读取操作只接收字符串路径，所以这个例子的参数类型为 `dict[str, str]`。

```python
class TextContent(TypedDict):
    type: Literal["text"]
    text: str


class ToolCall(TypedDict):
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: dict[str, str]


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: list[TextContent | ToolCall]
```

`TextContent | ToolCall` 表示每项可以是文字或调用。外层 `role` 说明这次输出来自助手，内层 `type` 说明具体内容是什么。调用也是模型提出的，因此仍属于助手消息。

然后是程序交回的结果。Pi 的 [ToolResultMessage](../../../references/pi/packages/ai/src/types.ts#L635) 将角色标记为 `toolResult`；[结果构造函数](../../../references/pi/packages/agent/src/agent-loop.ts#L940) 把请求的 `id` 写入结果的 `toolCallId`，并通过 `isError` 表达失败状态。

Python 沿用这项关联，将字段改为常见的下划线命名。本例返回的是文件文本或错误说明，结果内容使用字符串：

```python
class ToolResultMessage(TypedDict):
    role: Literal["toolResult"]
    tool_call_id: str
    content: str
    is_error: bool


Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
```

最后一行表示一条消息可以是这四类中的任意一种。各类型保留自己需要的字段：助手保存混合内容，工具结果保存调用关联与状态。

将上面的代码块依次放进 `messages.py`，就得到了这个例子所需的完整消息定义。下面把它们组合成一段记录；模型请求、文件内容和错误均为手工构造的数据：

```python
history: list[Message] = [
    {
        "role": "system",
        "content": "回答要说明依据，无法确认时如实说明。可请求 read_file(path)，path 是路径字符串。",
    },
    {"role": "user", "content": "检查 README.md 与其中提到的实现是否一致。"},
    {
        "role": "assistant",
        "content": [{
            "type": "toolCall", "id": "read-intro", "name": "read_file",
            "arguments": {"path": "README.md"},
        }],
    },
    {
        "role": "toolResult", "tool_call_id": "read-intro",
        "content": "a.py 提供读取函数；b.py 提供写入函数。", "is_error": False,
    },
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "我再检查这两个实现。"},
            {
                "type": "toolCall", "id": "read-a", "name": "read_file",
                "arguments": {"path": "a.py"},
            },
            {
                "type": "toolCall", "id": "read-b", "name": "read_file",
                "arguments": {"path": "b.py"},
            },
        ],
    },
    {
        "role": "toolResult", "tool_call_id": "read-a",
        "content": "def read(): return 'ok'", "is_error": False,
    },
    {
        "role": "toolResult", "tool_call_id": "read-b",
        "content": "文件不存在：b.py", "is_error": True,
    },
    {
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": "a.py 的 read() 返回固定字符串，并未读取文件，与说明不符；b.py 未找到，无法核对写入实现。",
        }],
    },
]
```

这份记录可以从头追踪：第一次读取获得文件线索，第二次助手输出根据线索提出两个请求，两个结果分别引用 `read-a` 和 `read-b`，最后的回答同时使用成功内容与失败状态。

Pi 的 [工具处理路径](../../../references/pi/packages/agent/src/agent-loop.ts#L268) 从助手内容中筛选调用，并将执行结果加入消息列表，下一次模型请求便能取得结果。消息类型描述了这条数据流需要交接的信息，实际执行由调用代码完成。

`TypedDict` 不自动检查外部数据。实施时，接收消息的边界要检查角色、内容形状和字段类型；处理工具记录时，还要检查请求标识是否重复、结果是否指向已知请求，以及一项请求是否被重复交回结果。

本课涉及 `messages.py` 的定义、`tests/test_messages.py` 的交互样例与上述契约检查，以及对应课程笔记和 README。现有文本 Fake 的输入范围保持明确，不能让它接受自己不会处理的工具消息。验证以这段交互为主：检查四类消息、两种助手内容、请求与结果的对应，以及成功和失败路径，特别是未知或重复关联不能被当成正常记录。

随后可以沿着已经出现的请求与结果，研究调用代码怎样执行一次读取并构造对应的工具结果。
