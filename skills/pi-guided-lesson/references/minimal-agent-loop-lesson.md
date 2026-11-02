# 最小 Agent：一次工具调用怎样完成

备课信息：本例按 learn-claude-code 固定版本 `ce8f9f186058939da54c9d6fead78dfb5d0fd6c3` 的 [s01](../../../references/learn-claude-code/s01_agent_loop/README.zh.md) 粒度组织；Pi 源码固定为 `7c22a93d0ed02092142fa63823e63d76aa4e2406`。代码是可独立运行的教学方案，尚未写入项目运行时。本段不作为课堂开场。

---

## 一、模型提出操作之后

用户问：“2 + 3 等于几？”

假设模型决定使用一个加法工具。模型可以提出“调用 `add`，参数是 2 和 3”，但这段请求本身还没有执行加法。程序需要接过请求、运行函数，再把结果交回模型，模型才能继续作答。

手动操作时，你可以把模型的请求交给程序，再把程序返回的 `5` 贴回去。让程序接手这个来回，就形成了一次完整交互：

```text
应用要求：计算时使用加法工具。
用户：2 + 3 等于几？
   ↓
模型：请求 add(left=2, right=3)，标识 call-1
   ↓
程序：执行 add，得到 5
   ↓
程序：把 call-1 的结果 5 交回模型
   ↓
模型：计算结果是 5。
```

程序需要分辨用户问题、模型请求和工具结果。模型一次响应也可能直接给出回答，因此还要区分“文字回答”和“工具请求”：有请求就执行并继续；只有文字就返回给用户。

请求和结果之间用同一个标识连接。接收结果时，模型能够确认 `5` 属于 `call-1`，而不必根据文字猜测。

## 二、把交互写成代码

### 1. 表达交接的数据

Pi 的 [SystemMessage](../../../references/pi/packages/ai/src/types.ts#L556) 表达应用要求，[UserMessage 和 AssistantMessage](../../../references/pi/packages/ai/src/types.ts#L574) 用角色区分用户输入与模型输出；助手内容中可以包含 [TextContent 和 ToolCall](../../../references/pi/packages/ai/src/types.ts#L422)。程序交回的 [ToolResultMessage](../../../references/pi/packages/ai/src/types.ts#L635) 使用 `toolCallId` 关联请求。

下面用 Python 字典表达这个例子。`TypedDict` 描述字典形状，`Literal` 标记角色和内容类型；实际对象仍是普通字典。

```python
from collections.abc import Callable
from typing import Literal, TypedDict


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str


class TextContent(TypedDict):
    type: Literal["text"]
    text: str


class ToolCall(TypedDict):
    type: Literal["toolCall"]
    id: str
    name: str
    arguments: dict[str, int]


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: list[TextContent | ToolCall]


class ToolResultMessage(TypedDict):
    role: Literal["toolResult"]
    tool_call_id: str
    content: str


Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
```

一次加法请求需要操作名、两个参数和请求标识。工具结果使用 `tool_call_id` 带回该标识，内容保存计算结果。

### 2. 模拟模型的两次响应

真实模型能根据输入生成响应。为了观察循环，我们先使用确定性的 Fake：收到指定问题时提出加法请求；收到对应结果时给出回答。

```python
def fake_model(history: list[Message]) -> AssistantMessage:
    if not history or history[0]["role"] != "system":
        raise ValueError("Fake Model 缺少应用要求")

    last = history[-1]

    if last["role"] == "user" and last["content"] == "你好":
        return {"role": "assistant", "content": [
            {"type": "text", "text": "你好。"}
        ]}

    if last["role"] == "user" and last["content"] == "2 + 3 等于几？":
        return {"role": "assistant", "content": [{
            "type": "toolCall", "id": "call-1", "name": "add",
            "arguments": {"left": 2, "right": 3},
        }]}

    if last["role"] == "toolResult" and last["tool_call_id"] == "call-1":
        return {"role": "assistant", "content": [
            {"type": "text", "text": f"计算结果是 {last['content']}。"}
        ]}

    raise ValueError("Fake Model 无法处理当前输入")
```

Fake 的回答是预先编写的；它检查应用要求是否存在，并让输入、工具请求和结果可重复观察。它并不会理解应用要求中的文字。

### 3. 执行模型提出的操作

程序根据工具名找到操作，从参数中取出两个数。结果带回请求的 `id`：

```python
def execute(call: ToolCall) -> ToolResultMessage:
    if call["name"] != "add":
        raise ValueError("未知工具")

    args = call["arguments"]
    if (
        not isinstance(args, dict)
        or set(args) != {"left", "right"}
        or any(type(args[key]) is not int for key in ("left", "right"))
    ):
        raise ValueError("加法参数必须是两个整数")

    return {
        "role": "toolResult",
        "tool_call_id": call["id"],
        "content": str(args["left"] + args["right"]),
    }
```

未知工具或非法参数直接抛出异常。只有计算成功，才构造正常工具结果。

### 4. 结果交回模型，直到出现回答

Pi 的 [AgentLoop](../../../references/pi/packages/agent/src/agent-loop.ts#L250) 取得助手响应后，从内容中筛选工具调用；执行结果加入上下文，后续请求再使用这个上下文。[结果构造函数](../../../references/pi/packages/agent/src/agent-loop.ts#L940) 将调用 `id` 放入结果的 `toolCallId`。

本例的 Fake 只产生完整的预设响应，工具失败时直接抛出异常。Pi 还会在分派前处理模型错误、取消或截断；真实模型接入时，不能把不完整的请求当成正常调用。

Python 中，调用方维护历史，循环每次把它交给模型：

```python
def run(
    history: list[Message],
    model: Callable[[list[Message]], AssistantMessage],
) -> str:
    for _ in range(4):
        answer = model(history)
        history.append(answer)
        calls = [item for item in answer["content"] if item["type"] == "toolCall"]

        if not calls:
            texts = [item["text"] for item in answer["content"] if item["type"] == "text"]
            if not texts:
                raise ValueError("模型没有返回回答")
            return "".join(texts)

        for call in calls:
            history.append(execute(call))

    raise RuntimeError("超过模型调用次数限制")
```

`model` 是一个函数参数：传入 `fake_model` 就使用模拟模型。这个接缝也让测试可以观察每次模型收到的历史。

### 试一下

将上面的代码块依次放进同一个临时脚本，再加入：

```python
history: list[Message] = [
    {"role": "system", "content": "计算时使用加法工具。"},
    {"role": "user", "content": "2 + 3 等于几？"},
]
model_inputs: list[list[Message]] = []


def observed_model(messages: list[Message]) -> AssistantMessage:
    model_inputs.append(list(messages))
    return fake_model(messages)


answer = run(history, observed_model)
print(answer)

assert answer == "计算结果是 5。"
assert len(model_inputs) == 2
assert model_inputs[1][-1] == {
    "role": "toolResult", "tool_call_id": "call-1", "content": "5"
}
assert [message["role"] for message in history] == [
    "system", "user", "assistant", "toolResult", "assistant"
]

direct: list[Message] = [
    {"role": "system", "content": "计算时使用加法工具。"},
    {"role": "user", "content": "你好"},
]
assert run(direct, fake_model) == "你好。"
```

预期输出：

```text
计算结果是 5。
```

第二次模型输入的最后一条消息，就是程序执行 `call-1` 得到的结果。直接回答则不经过工具。

本课将消息定义、Fake、工具函数和循环作为一个整体实现并验证。课堂示例使用固定响应和加法；未知工具、非法参数与模型调用次数超限均明确报错。下一步可以研究真实 Provider 怎样将模型返回的数据转换为这些消息，再处理实际调用中的失败状态。
