#!/usr/bin/env python3
"""s01: Agent Loop，改编自 learn-claude-code/s01_agent_loop/code.py。

自动加载项目根目录的 .env，运行：
    uv run experiments/s01_agent_loop.py

需要支持 Chat Completions 和工具调用的服务。
模型生成的命令以当前进程权限执行；本脚本不是沙箱。
"""

import json
import os
import subprocess
from pathlib import Path

try:
    import readline

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI


ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use bash to solve tasks. Act, don't explain."
)
MAX_TURNS = 20

# -- Tool definition: just bash --
TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}]


# -- Tool execution --
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, errors="replace", timeout=120)
        out = (r.stdout + r.stderr).strip()
        if len(out) > 50000:
            out = out[:50000] + "\n[output truncated]"
        if r.returncode != 0:
            return f"Error: Exit code {r.returncode}\n{out}"
        return out if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except OSError as e:
        return f"Error: {e}"


# -- Call tools, feed results back, repeat until the model stops --
def agent_loop(messages: list, client: OpenAI, model: str):
    for _ in range(MAX_TURNS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS,
            max_completion_tokens=8000,
        )
        choice = response.choices[0]
        message = choice.message
        messages.append(message.model_dump(exclude_none=True))

        # 截断或其他异常结束时，不执行可能不完整的工具调用。
        if choice.finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError(f"Model stopped unexpectedly: {choice.finish_reason}")

        tool_calls = message.tool_calls or []
        if not tool_calls:
            if choice.finish_reason != "stop":
                raise RuntimeError("Model requested tools but returned no tool calls")
            return
        if choice.finish_reason != "tool_calls":
            raise RuntimeError("Tool calls require finish_reason=tool_calls")

        for call in tool_calls:
            if call.type != "function" or call.function.name != "bash":
                raise ValueError(f"Unknown tool call: {call}")
            arguments = json.loads(call.function.arguments)
            if (not isinstance(arguments, dict) or set(arguments) != {"command"}
                    or not isinstance(arguments["command"], str)
                    or not arguments["command"].strip()):
                raise ValueError("bash requires one non-empty string argument: command")
            command = arguments["command"]
            print(f"\033[33m$ {command}\033[0m")
            output = run_bash(command)
            print(output[:200])
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
            })

    raise RuntimeError(f"Model call limit reached ({MAX_TURNS})")


# -- Entry point --
if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    MODEL = os.environ["MODEL_ID"]
    print("s01: Agent Loop (OpenAI Chat Completions)")
    print("Shell commands run with your process permissions. This is not a sandbox.")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    with OpenAI() as client:
        history = [{"role": "system", "content": SYSTEM}]
        while True:
            try:
                query = input("\001\033[36m\002s01 >> \001\033[0m\002")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            history.append({"role": "user", "content": query})
            agent_loop(history, client, MODEL)
            print(history[-1].get("content") or "")
            print()
