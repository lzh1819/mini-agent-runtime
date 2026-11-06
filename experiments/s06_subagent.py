#!/usr/bin/env python3
"""s06: Subagent，改编自 learn-claude-code/s06_subagent/code.py。

自动加载项目根目录的 .env，运行：
    uv run experiments/s06_subagent.py

task 工具启动独立对话，只把子 Agent 的最终文本交回主对话。
父子共享工作目录和 Hook；子 Agent 没有 task 工具，不能继续委派。
权限规则仍是教学示例，不是沙箱。
"""

import glob
import json
import os
import re
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
    "Use task for focused exploration or a self-contained subtask. "
    "All destructive operations require user approval."
)
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Complete the given task, then return a concise final answer. "
    "All destructive operations require user approval."
)
MAX_TURNS = 20
SUB_MAX_TURNS = 30


# -- From s02: tool implementations; permissions move before dispatch --
def run_bash(command: str) -> str:
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


def run_read(path: str, limit: int | None = None) -> str:
    lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
    if limit is not None and limit < len(lines):
        lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
    return "\n".join(lines)


def run_write(path: str, content: str) -> str:
    file_path = (WORKDIR / path).resolve()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content.encode('utf-8'))} bytes to {path}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    if not old_text:
        raise ValueError("old_text must not be empty")
    file_path = (WORKDIR / path).resolve()
    text = file_path.read_text(encoding="utf-8")
    if old_text not in text:
        raise ValueError(f"text not found in {path}")
    file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Edited {path}"


def run_glob(pattern: str) -> str:
    matches = sorted({
        match for match in glob.glob(pattern, root_dir=WORKDIR, recursive=True)
        if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
    })
    shown = matches[:200]
    if len(matches) > 200:
        shown.append("... (more matches omitted; narrow the pattern)")
    return "\n".join(shown) if shown else "(no matches)"


# -- Five tool declarations, using the OpenAI function format --
BASE_TOOLS = [
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

# -- From s02: dispatch map --
BASE_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# -- From s03: permission pipeline --
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/"]


def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


PERMISSION_RULES = [
    {"tools": ["read_file", "write_file", "edit_file"],
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     "message": "Access outside workspace"},
    {"tools": ["bash"],
     "check": lambda args: contains_destructive_command(args.get("command", "")) or
     any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]


def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n\033[33m[permission] {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


def check_permission(tool_name: str, args: dict) -> bool:
    if tool_name == "bash":
        reason = check_deny_list(args.get("command", ""))
        if reason:
            print(f"\n\033[31m[blocked] {reason}\033[0m")
            return False
    reason = check_rules(tool_name, args)
    if reason:
        return ask_user(tool_name, args, reason) == "allow"
    return True


# -- New in s04: callbacks registered at fixed points --
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def permission_hook(tool_name: str, args: dict):
    if not check_permission(tool_name, args):
        return "Permission denied."
    return None


def log_hook(tool_name: str, args: dict):
    args_preview = str(list(args.values())[:2])[:60]
    print(f"\033[90m[HOOK] {tool_name}({args_preview})\033[0m")
    return None


def large_output_hook(tool_name: str, args: dict, output: str):
    if len(output) > 100000:
        print(f"\033[33m[HOOK] Large output from {tool_name}: {len(output)} chars\033[0m")
    return None


def context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    tool_count = sum(m["role"] == "tool" for m in messages)
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# -- Parent and child tools go through the same hooks and dispatch --
def execute_tool(call, handlers: dict) -> str:
    name = call.function.name
    arguments = json.loads(call.function.arguments)
    blocked = trigger_hooks("PreToolUse", name, arguments)
    if blocked is not None:
        return str(blocked)

    try:
        handler = handlers.get(name)
        output = handler(**arguments) if handler else f"Error: Unknown tool: {name}"
    except (OSError, ValueError, TypeError) as e:
        output = f"Error: {e}"
    trigger_hooks("PostToolUse", name, arguments, output)
    return output


# -- New in s06: a nested loop with fresh messages and base tools only --
SUB_TOOLS = list(BASE_TOOLS)
SUB_HANDLERS = dict(BASE_HANDLERS)


def run_subagent(prompt: str, client: OpenAI, model: str) -> str:
    print("\n\033[35m[Subagent started]\033[0m")
    messages = [
        {"role": "system", "content": SUB_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    for _ in range(SUB_MAX_TURNS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=SUB_TOOLS,
            max_completion_tokens=8000,
        )
        choice = response.choices[0]
        message = choice.message
        messages.append(message.model_dump(exclude_none=True))

        if choice.finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError(f"Model stopped unexpectedly: {choice.finish_reason}")
        tool_calls = message.tool_calls or []
        if not tool_calls:
            force = trigger_hooks("Stop", messages)
            if force is not None:
                messages.append({"role": "user", "content": str(force)})
                continue
            print("\033[35m[Subagent done]\033[0m")
            return message.content or "(no summary)"

        for call in tool_calls:
            output = execute_tool(call, SUB_HANDLERS)
            print(f"  \033[90m[sub] {call.function.name}: {output[:100]}\033[0m")
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": output,
            })

    raise RuntimeError(f"Subagent model call limit reached ({SUB_MAX_TURNS})")


TASK_TOOL = {"type": "function", "function": {
    "name": "task",
    "description": "Run a subagent with fresh conversation context and return its final text.",
    "parameters": {
        "type": "object", "properties": {"prompt": {"type": "string", "minLength": 1}},
        "required": ["prompt"], "additionalProperties": False,
    },
}}
TOOLS = [*BASE_TOOLS, TASK_TOOL]


# -- Parent agent loop --
def agent_loop(messages: list, client: OpenAI, model: str):
    handlers = {**BASE_HANDLERS, "task": lambda prompt: run_subagent(prompt, client, model)}
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
            force = trigger_hooks("Stop", messages)
            if force is not None:
                messages.append({"role": "user", "content": str(force)})
                continue
            return

        for call in tool_calls:
            output = execute_tool(call, handlers)
            print(output[:200])
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": output,
            })

    raise RuntimeError(f"Model call limit reached ({MAX_TURNS})")


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    MODEL = os.environ["MODEL_ID"]
    print("s06: Subagent - fresh messages, final text returns (OpenAI Chat Completions)")
    print("Permission rules are a teaching example, not a sandbox.")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    with OpenAI() as client:
        history = [{"role": "system", "content": SYSTEM}]
        while True:
            try:
                query = input("\001\033[36m\002s06 >> \001\033[0m\002")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            trigger_hooks("UserPromptSubmit", query)
            history.append({"role": "user", "content": query})
            agent_loop(history, client, MODEL)
            print(history[-1].get("content") or "")
            print()
