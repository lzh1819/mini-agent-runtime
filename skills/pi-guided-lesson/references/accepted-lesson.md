# 用户认可的课堂样例：对话历史

样例整理自本项目对话中用户明确表示满意的版本，用于校准教学深度与表达密度。下面保留当时的设计和代码示意，不代表最新实现或验证结果；源码链接基于参考版本 `7c22a93d0ed02092142fa63823e63d76aa4e2406`。重新授课时核对当前代码，其他主题不要照搬这里的消息结构、校验策略或文件清单。这段说明不应作为课堂开场。

---

**一、第二次调用缺少了什么**

上一课的函数一次只接收一段文本：

```python
generate("我叫小林。")
generate("我叫什么名字？")
```

第二次调用收到的只有问题，没有“小林”这个信息。函数也没有保存第一次交互，因此无法获得回答所需的上下文。

最直接的补救，是由调用方保留之前说过的话，下次一起传入：

```python
[
    "我叫小林。",
    "你好，小林。",
    "我叫什么名字？",
]
```

这样保存了内容和顺序，但发言身份仍然依赖一个隐含约定：第一条来自用户，第二条来自助手，依次交替。

这个约定遇到连续补充就会失效：

```text
用户：帮我解释这个函数。
用户：重点解释参数。
助手：好的……
```

第二条仍然是用户输入，不能按位置把它判断成助手回答。因此，需要在记录内容时，同时保存它来自哪一方：

```python
{"role": "user", "content": "重点解释参数。"}
```

将这样的记录放入列表：

```python
history = [
    {"role": "user", "content": "帮我解释这个函数。"},
    {"role": "user", "content": "重点解释参数。"},
    {"role": "assistant", "content": "好的……"},
]
```

现在三项信息各有明确位置：

| 信息 | 保存方式 |
|---|---|
| 说了什么 | `content` |
| 谁说的 | `role` |
| 先后关系 | 列表顺序 |

身份不再依赖固定交替，历史也能完整交给后续调用。

当前不需要时间戳来表达顺序，列表已经足够；也没有按消息 ID 查找或修改记录的需求，因此先不增加这些字段。

**二、Pi 的对应实现与 Python 代码**

Pi 的 [UserMessage 和 AssistantMessage](../../../references/pi/packages/ai/src/types.ts#L574) 使用 `role` 区分参与方，由 `content` 承载内容；[Context.messages](../../../references/pi/packages/ai/src/types.ts#L781) 使用消息数组组织交互记录。

这些结构承担的，正是刚才推导出的内容、身份和顺序三项职责。

保存之后，还必须进入请求。在 Pi 的 [模型调用路径](../../../references/pi/packages/agent/src/agent-loop.ts#L390) 中，当前消息会被转换为模型层消息，再用于构建上下文并调用模型接口：

```text
当前消息 → 模型层消息 → 请求上下文 → 模型调用
```

所以，程序中有历史列表还不够；调用时必须把需要的历史交进去。

Pi 支持图像、思考和工具调用，因此内容结构比字符串复杂。当前只研究纯文本问答，保留角色和顺序，将内容限定为字符串即可。

在 `messages.py` 中，可以用标准库声明消息形状：

```python
from typing import Literal, TypedDict


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str
```

`TypedDict` 描述字典字段，`Literal` 描述允许的角色值。运行时消息仍然是普通字典，没有额外的消息对象生命周期。

它们帮助阅读代码和静态检查，但不会自动校验输入。因此，实际检查放在接收消息的 `generate()` 中。

`fake.py` 的接口由接收字符串改为接收消息列表：

```python
from .messages import Message


def generate(
    messages: list[Message],
    *,
    error: Exception | None = None,
) -> str:
    if not isinstance(messages, list):
        raise TypeError("messages 必须是消息列表")

    lines = []

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise TypeError(f"messages[{index}] 必须是消息字典")

        if message.keys() != {"role", "content"}:
            raise ValueError(
                f"messages[{index}] 必须只包含 role 和 content"
            )

        if message["role"] not in ("user", "assistant"):
            raise ValueError(
                f"messages[{index}].role 必须是 user 或 assistant"
            )

        if not isinstance(message["content"], str):
            raise TypeError(
                f"messages[{index}].content 必须是字符串"
            )

        lines.append(f"{message['role']}: {message['content']}")

    if error is not None:
        raise error

    return "模拟回答：\n" + "\n".join(lines)
```

这个函数做三件事：校验消息、按顺序读取内容、返回确定性的展示结果。`error` 延续上一课的失败模拟方式。

当前选择拒绝额外字段，是为了明确接口支持范围；以后增加字段时，类型声明与校验一起调整。消息内容中的空白和换行保持原样，也不要求角色严格交替。

Fake 展示全部输入，是为了让历史是否完整传入变得可观察。它不会理解问题，这份展示文本也不作为消息存储格式。

历史由调用方维护：

```python
history: list[Message] = [
    {"role": "user", "content": "你好"},
]

answer = generate(history)
history.append({"role": "assistant", "content": answer})

history.append({"role": "user", "content": "再介绍一下自己"})
next_answer = generate(history)
```

`generate()` 只读取当前输入；调用方负责追加用户消息和助手回答。这样不会出现函数内部与调用方重复记录回答的情况，也没有隐藏的跨调用状态。

返回值暂时仍是字符串，因为调用方已经知道它是本次回答，加入历史时可以补上 `assistant` 身份。以后返回值需要表达工具调用、停止原因等信息，再扩展响应结构。

测试需要覆盖：

- 多条消息的内容、角色和顺序完整保留。
- 调用没有修改输入历史。
- 第二次只传最新消息时，不会自动带入旧内容。
- 连续同角色消息可以处理。
- 非法消息与模拟失败明确报错。

这些测试验证数据契约，不验证真实模型理解上下文的能力。

本课对应 `messages.py`、`fake.py` 和测试文件；`lessons/001-conversation-history.md` 保存设计推导、真实答疑与实施结果，README 更新调用示例。工作区已有代码草稿，正式实施时需要核对并验证。

这份消息契约建立之后，接入真实 Provider 就有了明确起点：把内部的有序消息转换成供应商接受的请求格式，再处理返回结果。
