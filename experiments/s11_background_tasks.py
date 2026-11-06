#!/usr/bin/env python3
"""s11: Background Tasks，改编自 learn-claude-code/s11_background_tasks/code.py。

自动加载项目根目录的 .env，运行：
    uv run experiments/s11_background_tasks.py

显式后台 Bash 调用先返回任务 ID，后续轮次再收集结果通知。
后台线程执行命令，主线程检查权限；超时和程序退出时清理进程。
权限规则仍是教学示例，不是沙箱。
"""

import atexit
import glob
import json
import os
import re
import signal
import subprocess
import threading
import time
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
    "Use tools to solve tasks. Set run_in_background to true only for independent "
    "Bash commands. A background task ID means the command was started, not that it "
    "succeeded. All destructive operations require user approval."
)
MAX_TURNS = 20
BASH_TIMEOUT = 120


# -- Shell lifecycle, shared by foreground and background execution --
_shell_processes: set[subprocess.Popen] = set()
_shell_process_lock = threading.RLock()
_shell_stopping = False


def _stop_process_group(process: subprocess.Popen):
    """POSIX stops the process group; Windows stops the shell process."""
    if os.name == "nt":
        for stop in (process.terminate, process.kill):
            if process.poll() is not None:
                return
            try:
                stop()
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def _stop_all_shell_processes():
    global _shell_stopping
    with _shell_process_lock:
        # Prevent a queued worker from starting a process after this snapshot.
        _shell_stopping = True
        processes = list(_shell_processes)
    for process in processes:
        _stop_process_group(process)


def _handle_termination_signal(signum, _frame):
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


atexit.register(_stop_all_shell_processes)


def _run_bash_process(command: str) -> tuple[str, int | None]:
    process = None
    try:
        popen_kwargs = {} if os.name == "nt" else {"start_new_session": True}
        with _shell_process_lock:
            if _shell_stopping:
                return "Error: Shell runtime is shutting down", None
            process = subprocess.Popen(
                command, shell=True, cwd=WORKDIR,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", **popen_kwargs,
            )
            _shell_processes.add(process)
        stdout, stderr = process.communicate(timeout=BASH_TIMEOUT)
        output = (stdout + stderr).strip()
        if len(output) > 50000:
            output = output[:50000] + "\n[output truncated]"
        return output or "(no output)", process.returncode
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({BASH_TIMEOUT}s)", None
    except OSError as error:
        return f"Error: {type(error).__name__}: {error}", None
    finally:
        if process is not None:
            _stop_process_group(process)
            process.wait(timeout=0.2)
            process.stdout.close()
            process.stderr.close()
            with _shell_process_lock:
                _shell_processes.discard(process)


def _format_bash_result(output: str, exit_code: int | None) -> str:
    if exit_code in (0, None):
        return output
    return f"Error: command exited with status {exit_code}\n{output}"


def run_bash(command: str, run_in_background: bool = False) -> str:
    return _format_bash_result(*_run_bash_process(command))


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
TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {
            "type": "object", "properties": {
                "command": {"type": "string"}, "run_in_background": {"type": "boolean"},
            },
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
TOOL_HANDLERS = {
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


# -- New in s11: execute in a worker, publish once, collect on a later turn --
class BackgroundManager:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self._ready: list[str] = []
        self._counter = 0
        self._lock = threading.Lock()

    def start(self, call, arguments: dict) -> str:
        if call.function.name != "bash":
            raise ValueError("Only Bash commands can run in the background")
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Bash command cannot be empty")
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {
                "tool_call_id": call.id, "command": command, "status": "running",
            }
        thread = threading.Thread(target=self._run, args=(task_id, command), daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self.tasks.pop(task_id)
            raise
        print(f"  [background] started {task_id}: {command[:60]}")
        return task_id

    def _run(self, task_id: str, command: str):
        try:
            output, exit_code = _run_bash_process(command)
            result = _format_bash_result(output, exit_code)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as error:
            # Worker failures must reach the main loop through a failed notification.
            result = f"Error: {type(error).__name__}: {error}"
            status = "failed"
        with self._lock:
            self.tasks[task_id]["status"] = status
            self.results[task_id] = result
            self._ready.append(task_id)

    def collect(self) -> list[str]:
        with self._lock:
            ready = [(task_id, self.tasks.pop(task_id), self.results.pop(task_id))
                     for task_id in self._ready]
            self._ready.clear()
        notifications = []
        for task_id, task, result in ready:
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{result[:500]}</summary>\n"
                f"</task_notification>"
            )
            print(f"  [background] collected {task_id}: {task['status']}")
        return notifications


BACKGROUND = BackgroundManager()


def should_run_background(tool_name: str, arguments: dict) -> bool:
    return tool_name == "bash" and arguments.get("run_in_background") is True


def inject_background_results(messages: list) -> int:
    notifications = BACKGROUND.collect()
    if not notifications:
        return 0
    messages.append({"role": "user", "content": "\n\n".join(notifications)})
    return len(notifications)


def execute_tool(call) -> str:
    name = call.function.name
    arguments = json.loads(call.function.arguments)
    blocked = trigger_hooks("PreToolUse", name, arguments)
    if blocked is not None:
        return str(blocked)
    try:
        if should_run_background(name, arguments):
            task_id = BACKGROUND.start(call, arguments)
            output = (f"[Background task {task_id} started] "
                      "The result will be collected on a later turn.")
        else:
            handler = TOOL_HANDLERS.get(name)
            output = handler(**arguments) if handler else f"Error: Unknown tool: {name}"
    except (OSError, ValueError, TypeError) as error:
        output = f"Error: {error}"
    trigger_hooks("PostToolUse", name, arguments, output)
    return output


# -- Main loop: collect notifications before each model request --
def agent_loop(messages: list, client: OpenAI, model: str):
    for _ in range(MAX_TURNS):
        inject_background_results(messages)
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
            output = execute_tool(call)
            print(output[:200])
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": output,
            })

    raise RuntimeError(f"Model call limit reached ({MAX_TURNS})")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_termination_signal)
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    MODEL = os.environ["MODEL_ID"]
    print("s11: Background Tasks - explicit background Bash execution (OpenAI Chat Completions)")
    print("Permission rules are a teaching example, not a sandbox.")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    with OpenAI() as client:
        history = [{"role": "system", "content": SYSTEM}]
        while True:
            try:
                query = input("\001\033[36m\002s11 >> \001\033[0m\002")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            trigger_hooks("UserPromptSubmit", query)
            history.append({"role": "user", "content": query})
            agent_loop(history, client, MODEL)
            print(history[-1].get("content") or "")
            print()
