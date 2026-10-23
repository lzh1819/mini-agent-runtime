"""用于学习单次文本调用的确定性模型替身，不调用真实模型。"""


def generate(text: str, *, error: Exception | None = None) -> str:
    """按固定模板回答；error 仅用于显式模拟调用失败。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    if error is not None:
        raise error
    return f"模拟回答：{text}"
