# 消息：模型和程序怎样交接一次任务

备课信息：写法参照 learn-claude-code 固定版本 `ce8f9f186058939da54c9d6fead78dfb5d0fd6c3` 的 [s01](../../../references/learn-claude-code/s01_agent_loop/README.zh.md)、[s02](../../../references/learn-claude-code/s02_tool_use/README.zh.md)；消息语义依据 Pi `7c22a93d0ed02092142fa63823e63d76aa4e2406`。下文模型输出和读取结果是手工示例，类型是本课拟议定义，尚未获得逐项确认。本段不作为课堂开场。

---

## 一、一次问答，几次交接

用户问：“比较 a.txt 和 b.txt 的内容。”

模型收到了文件名，却没有文件内容。你可以自己读文件，把内容交给它；如果它还需要别的信息，你再取回来、再发给它。

现在希望程序接手这些来回。程序提供 `read_file(path)`，负责读取文件；模型按约定提出读取请求，程序执行后把结果交回。一次交互可能是：

```text
应用要求：依据实际内容回答，读取失败时说明原因。
用户：比较 a.txt 和 b.txt 的内容。
模型：我先读取这两个文件。
      请求 read_file(path="a.txt")，标识 read-a
      请求 read_file(path="b.txt")，标识 read-b
程序：read-a 读取成功，内容是“苹果”。
      read-b 读取失败，原因是文件不存在。
模型：a.txt 的内容是苹果；b.txt 无法读取，暂时不能比较。
```

程序要处理这段交互，不能只收到混在一起的文字。模型说“我先读取”，表示它提出了动作；只有程序执行后交回的记录，才表示读取已经发生。两者虽然都出现在对话中，接收方要做的事却不同。

因此，每条记录需要说明来源，并保存实际内容。用户的问题交给模型；模型的读取请求交给程序；程序的结果再交给模型。按交互顺序保存这些记录，下一次请求才能拿到前面的信息。

读取请求还需要明确“做什么、带什么参数”。`read_file` 是操作名，`path` 是参数。程序按这些信息执行，不必从“请帮我读一下这个文件”这样的句子里猜测。

两次操作都叫 `read_file`。返回结果中的 `read-a`、`read-b` 用于指出各自回答哪次请求；名字相同也不会混淆。成功与失败则单独标记，避免把错误说明误当作文件正文。

这段交互里，模型同时产生了一句话和两个操作请求。所以助手输出既要容纳文字，也要容纳供程序处理的结构化数据。它们都来自模型，但用途不同。

## 二、把每次交接写成代码

### 1. 记录应用要求和用户问题

应用给出持续有效的行为要求，用户给出本次任务。Pi 用 [SystemMessage](../../../references/pi/packages/ai/src/types.ts#L556) 和 [UserMessage](../../../references/pi/packages/ai/src/types.ts#L574) 区分它们，`role` 表达来源，`content` 保存内容。

Python 可以用 `TypedDict` 描述字典字段，`Literal` 表达固定的角色值：

```python
from typing import Literal, TypedDict


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str
```

例如，`{"role": "user", "content": "比较 a.txt 和 b.txt 的内容。"}` 就明确记录了一次用户输入。这里的消息仍然是普通字典，类型声明方便阅读与检查，不会自行执行校验。

### 2. 记录模型的话和读取请求

Pi 的 [AssistantMessage](../../../references/pi/packages/ai/src/types.ts#L580) 将内容组织成列表。列表中的每项带有 `type`：[TextContent](../../../references/pi/packages/ai/src/types.ts#L422) 保存文字，[ToolCall](../../../references/pi/packages/ai/src/types.ts#L444) 保存工具调用。

这正好可以表示同一次输出中的一句话和两个请求。选择内容列表，是为了把不同用途的内容及其顺序放在同一份输出中。

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

`name` 与 `arguments` 告诉程序执行什么，`id` 标记这是哪一次请求。本例的读取工具只接收字符串路径，因此参数写为 `dict[str, str]`。`TextContent | ToolCall` 表示每项可以是文字或调用。

把模型的这次输出写出来：

```python
call_a: ToolCall = {
    "type": "toolCall", "id": "read-a", "name": "read_file",
    "arguments": {"path": "a.txt"},
}
call_b: ToolCall = {
    "type": "toolCall", "id": "read-b", "name": "read_file",
    "arguments": {"path": "b.txt"},
}
request: AssistantMessage = {
    "role": "assistant",
    "content": [
        {"type": "text", "text": "我先读取这两个文件。"},
        call_a,
        call_b,
    ],
}
```

程序接收 `request` 后，读取每项的 `type`。文字用于展示；遇到 `toolCall`，就能取出操作名和参数。Pi 的 [工具处理路径](../../../references/pi/packages/agent/src/agent-loop.ts#L268) 正是在助手内容中筛选调用，再交给执行管线。

外层 `role` 回答“谁产生的”，内层 `type` 回答“这项内容是什么”。工具调用由模型提出，因此放在助手消息里。

### 3. 把执行结果交回来

Pi 用 [ToolResultMessage](../../../references/pi/packages/ai/src/types.ts#L635) 记录执行结果。[结果构造函数](../../../references/pi/packages/agent/src/agent-loop.ts#L940) 把请求的 `id` 写入结果的 `toolCallId`，同时保留 `isError`。

Python 采用下划线命名；结果中的文本复用刚才定义的 `TextContent`：

```python
class ToolResultMessage(TypedDict):
    role: Literal["toolResult"]
    tool_call_id: str
    content: list[TextContent]
    is_error: bool
```

两个读取结果分别是：

```python
result_a: ToolResultMessage = {
    "role": "toolResult", "tool_call_id": "read-a",
    "content": [{"type": "text", "text": "苹果"}],
    "is_error": False,
}
result_b: ToolResultMessage = {
    "role": "toolResult", "tool_call_id": "read-b",
    "content": [{"type": "text", "text": "文件不存在：b.txt"}],
    "is_error": True,
}
```

`result_b` 的 `tool_call_id` 指向 `call_b`，因此能确定失败的是读取 b.txt 的请求。`content` 说明原因，`is_error` 明确标记失败。

### 4. 组合成一次完整交互

消息列表需要容纳应用指令、用户问题、助手输出和工具结果。用联合类型描述这四种可能：

```python
Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage

history: list[Message] = [
    {
        "role": "system",
        "content": "依据实际内容回答；可请求 read_file(path)，读取失败时说明原因。",
    },
    {"role": "user", "content": "比较 a.txt 和 b.txt 的内容。"},
    request,
    result_a,
    result_b,
    {
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": "a.txt 的内容是苹果；b.txt 无法读取，暂时不能比较。",
        }],
    },
]
```

第一次模型请求携带应用指令和用户问题。程序取得 `request`，执行后将请求与对应结果追加到列表，再次调用模型；最后一条就是这次模型回答的记录。

上面的类型块组成 `messages.py` 的定义；请求、结果与历史组成一份交互样例。模型接口会负责将实际响应转换成这些内部类型。

### 试一下

将上面的 Python 代码块依次放入一个临时脚本，追加以下断言。它们只使用样例数据，不会读取真实文件：

```python
calls = {
    item["id"]: item
    for item in request["content"]
    if item["type"] == "toolCall"
}
assert calls[result_b["tool_call_id"]]["arguments"]["path"] == "b.txt"
assert result_b["is_error"] is True
assert result_a["content"][0]["text"] == "苹果"
assert [message["role"] for message in history] == [
    "system", "user", "assistant", "toolResult", "toolResult", "assistant"
]
```

观察两个位置：调用与结果怎样通过同一个标识关联；同一条助手消息里的文字和调用怎样通过 `type` 区分。

本课的实施范围是消息定义、相应输入校验、交互样例及其测试，并同步课程笔记和 README。当前文本 Fake 只能接收自己支持的纯文本消息，调整类型时需要同步其输入声明和检查。

接下来可以沿着 `ToolCall` 到 `ToolResultMessage` 的这段交接，实现一次实际读取：按名称找到函数，检查参数，执行，再把成功或失败构造成对应结果。
