import pytest

from mini_agent_runtime.fake import generate


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你好", "模拟回答：你好"),
        ("", "模拟回答："),
        ("  第一行\n第二行  ", "模拟回答：  第一行\n第二行  "),
    ],
)
def test_generate_returns_complete_text(text, expected):
    assert generate(text) == expected


@pytest.mark.parametrize("text", [None, 42, ["你好"]])
def test_generate_rejects_non_text_input(text):
    with pytest.raises(TypeError, match="text 必须是字符串"):
        generate(text)


@pytest.mark.parametrize("error", [RuntimeError("模拟失败"), TimeoutError("模拟超时")])
def test_generate_raises_simulated_failure(error):
    with pytest.raises(type(error)) as caught:
        generate("你好", error=error)
    assert caught.value is error
