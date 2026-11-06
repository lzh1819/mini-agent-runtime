#!/usr/bin/env python3
"""s02: Tool Use，改编自 learn-claude-code/s02_tool_use/code.py。

自动加载项目根目录的 .env，运行：
    uv run experiments/s02_tool_use.py

在 S01 循环上增加四个文件工具和 TOOL_HANDLERS 字典分发。
文件工具限制在启动目录内；Shell 仍以进程权限运行，不是沙箱。
"""

import glob
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


WORKDIR = Path.cwd().resolve()
ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use tools to solve tasks. Act, don't explain."
)
MAX_TURNS = 20


# -- From s01 --
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
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


# -- New in s02: four tools --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None) -> str:
    lines = safe_path(path).read_text(encoding="utf-8").splitlines()
    if limit is not None and limit < len(lines):
        lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
    return "\n".join(lines)


def run_write(path: str, content: str) -> str:
    file_path = safe_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content.encode('utf-8'))} bytes to {path}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    if not old_text:
        raise ValueError("old_text must not be empty")
    file_path = safe_path(path)
    text = file_path.read_text(encoding="utf-8")
    if old_text not in text:
        raise ValueError(f"text not found in {path}")
    file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Edited {path}"


def run_glob(pattern: str) -> str:
    safe_path(pattern)
    matches = sorted({
        match for match in glob.glob(pattern, root_dir=WORKDIR, recursive=True)
        if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
    })
    shown = matches[:200]
    if len(matches) > 200:
        shown.append("... (more matches omitted; narrow the pattern)")
    return "\n".join(shown) if shown else "(no matches)"


# -- Five tool declarations, using the OpenAI function format --
TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {
            "type": "object", "properties": {"command": {"type": "string"}},
            "required": ["command"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {
            "type": "object", "properties": {
                "path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file.",
        "parameters": {
            "type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"},
            },
            "required": ["path", "content"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace exact text in a file once.",
        "parameters": {
            "type": "object", "properties": {
                "path": {"type": "string"}, "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
        "parameters": {
            "type": "object", "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"], "additionalProperties": False,
        },
    }},
]

# -- New in s02: dispatch map --
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# -- Same loop as s01, with dictionary dispatch --
def agent_loop(messages: list, client: OpenAI, model: str):
    for _ in range(MAX_TURNS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS,
            max_completion_tokens=8000,
        )
        choice = response.choices[0]
        message = choice.message
        messages.append(message.model_dump(exclude_none=True))

        if choice.finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError(f"Model stopped unexpectedly: {choice.finish_reason}")
        tool_calls = message.tool_calls or []
        if not tool_calls:
            return

        for call in tool_calls:
            name = call.function.name
            print(f"\033[33m> {name}\033[0m")
            try:
                handler = TOOL_HANDLERS.get(name)
                arguments = json.loads(call.function.arguments)
                output = handler(**arguments) if handler else f"Error: Unknown tool: {name}"
            except (OSError, ValueError, TypeError) as e:
                output = f"Error: {e}"
            print(output[:200])
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": output,
            })

    raise RuntimeError(f"Model call limit reached ({MAX_TURNS})")


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    MODEL = os.environ["MODEL_ID"]
    print("s02: Tool Use (OpenAI Chat Completions)")
    print("Shell commands run with your process permissions. This is not a sandbox.")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    with OpenAI() as client:
        history = [{"role": "system", "content": SYSTEM}]
        while True:
            try:
                query = input("\001\033[36m\002s02 >> \001\033[0m\002")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            history.append({"role": "user", "content": query})
            agent_loop(history, client, MODEL)
            print(history[-1].get("content") or "")
            print()
