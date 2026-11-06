#!/usr/bin/env python3
"""s10: Task System，改编自 learn-claude-code/s10_task_system/code.py。

自动加载项目根目录的 .env，运行：
    uv run experiments/s10_task_system.py

工作目录的 .tasks/ 保存任务、负责人和依赖，重启后可继续读取。
先创建任务取得 ID，再建立依赖；前置任务完成后才能认领下游任务。
保留基础工具和 Hook；权限规则仍是教学示例，不是沙箱。
"""

import glob
import json
import os
import re
import secrets
import subprocess
from dataclasses import asdict, dataclass
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
    "Use task tools to track dependencies and progress. Create all task nodes first. "
    "After create_task returns runtime-generated IDs, use update_task with those exact "
    "IDs to add dependencies. All destructive operations require user approval."
)
MAX_TURNS = 20


# -- New in s10: persistent tasks and a dependency graph --
TASKS_DIR = WORKDIR / ".tasks"
TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]


class TaskStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _root(self, create: bool = False) -> Path:
        root = self.directory.resolve()
        if not root.is_relative_to(WORKDIR.resolve()):
            raise ValueError("Task store escapes the workspace")
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        root = self._root(create=create_root)
        path = root / f"{task_id}.json"
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Task path escapes the store or is a symlink: {task_id}")
        return path

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")
        self._root(create=True)
        for _ in range(100):
            task = Task(f"task_{secrets.token_hex(4)}", subject, description,
                        "pending", None, [])
            try:
                with self._path(task.id).open("x", encoding="utf-8") as handle:
                    json.dump(asdict(task), handle, ensure_ascii=False, indent=2)
                return task
            except FileExistsError:
                continue
        raise RuntimeError("Could not allocate a unique task ID")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str, add_blocked_by: list[str]) -> Task:
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list of task IDs")
        task = self.load(task_id)
        if task.status != "pending" or task.owner is not None:
            raise ValueError("Dependencies can only be updated while pending and unowned")
        dependencies = list(dict.fromkeys(add_blocked_by))
        for dependency in dependencies:
            if dependency == task_id:
                raise ValueError("Task cannot depend on itself")
            if not self.exists(dependency):
                raise ValueError(f"Dependency not found: {dependency}")
            if dependency not in task.blockedBy and self._depends_on(dependency, task_id):
                raise ValueError(f"Dependency cycle detected: {task_id} -> {dependency}")
        task.blockedBy.extend(dependency for dependency in dependencies
                              if dependency not in task.blockedBy)
        self.save(task)
        return task

    def save(self, task: Task):
        self._path(task.id, create_root=True).write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def load(self, task_id: str) -> Task:
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        task = Task(**data)
        if task.id != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        if task.status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid task status: {task.status}")
        return task

    def list(self) -> list[Task]:
        return [self.load(path.stem) for path in sorted(self._root().glob("task_*.json"))]


TASKS = TaskStore(TASKS_DIR)


def create_task(subject: str, description: str = "") -> Task:
    return TASKS.create(subject, description)


def update_task(task_id: str, addBlockedBy: list[str]) -> Task:
    return TASKS.update_dependencies(task_id, addBlockedBy)


def load_task(task_id: str) -> Task:
    return TASKS.load(task_id)


def list_tasks() -> list[Task]:
    return TASKS.list()


def get_task(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), ensure_ascii=False, indent=2)


def incomplete_dependencies(task: Task) -> list[str]:
    incomplete = []
    for dependency in task.blockedBy:
        try:
            if load_task(dependency).status != "completed":
                incomplete.append(dependency)
        except FileNotFoundError:
            incomplete.append(dependency)
    return incomplete


def can_start(task_id: str) -> bool:
    return not incomplete_dependencies(load_task(task_id))


def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    dependencies = incomplete_dependencies(task)
    if dependencies:
        return f"Blocked by: {dependencies}"
    task.owner = owner
    task.status = "in_progress"
    TASKS.save(task)
    print(f"  [claim] {task.subject} -> in_progress (owner: {owner})")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    if task.owner != owner:
        return f"Task {task_id} is owned by {task.owner}, not {owner}"
    ready_before = {candidate.id for candidate in list_tasks()
                    if candidate.status == "pending" and candidate.blockedBy
                    and can_start(candidate.id)}
    task.status = "completed"
    TASKS.save(task)
    unblocked = [candidate.subject for candidate in list_tasks()
                 if candidate.status == "pending" and candidate.blockedBy
                 and candidate.id not in ready_before and can_start(candidate.id)]
    print(f"  [complete] {task.subject}")
    message = f"Completed {task.id} ({task.subject})"
    if unblocked:
        message += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  [unblocked] {', '.join(unblocked)}")
    return message


def run_create_task(subject: str, description: str = "") -> str:
    task = create_task(subject, description)
    return f"Created {task.id}: {task.subject}"


def run_update_task(task_id: str, addBlockedBy: list[str]) -> str:
    task = update_task(task_id, addBlockedBy)
    return f"Updated {task.id} blockedBy: {', '.join(task.blockedBy) or '(none)'}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for task in tasks:
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[task.status]
        dependencies = f" (blockedBy: {', '.join(task.blockedBy)})" if task.blockedBy else ""
        owner = f" [{task.owner}]" if task.owner else ""
        lines.append(f"{marker} {task.id}: {task.subject} [{task.status}]{owner}{dependencies}")
    return "\n".join(lines)


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id, owner="agent")


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


# -- Base and task tools, using the OpenAI function format --
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
    {"type": "function", "function": {
        "name": "create_task", "description": "Create a task and return its runtime-generated ID.",
        "parameters": {
            "type": "object", "properties": {
                "subject": {"type": "string"}, "description": {"type": "string"},
            },
            "required": ["subject"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "update_task", "description": "Add dependencies using IDs returned by create_task.",
        "parameters": {
            "type": "object", "properties": {
                "task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
                "addBlockedBy": {"type": "array", "minItems": 1,
                                 "items": {"type": "string", "pattern": TASK_ID_PATTERN.pattern}},
            },
            "required": ["task_id", "addBlockedBy"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "list_tasks", "description": "List tasks with status, owner, and dependencies.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "get_task", "description": "Get a task by ID, including its full description.",
        "parameters": {
            "type": "object", "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "claim_task", "description": "Claim a pending task whose dependencies are complete.",
        "parameters": {
            "type": "object", "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "complete_task", "description": "Complete the task claimed by this agent.",
        "parameters": {
            "type": "object", "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"], "additionalProperties": False,
        },
    }},
]

# -- From s02: dispatch map --
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    "create_task": run_create_task, "update_task": run_update_task,
    "list_tasks": run_list_tasks, "get_task": get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
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


# -- Same loop as s03, with hooks replacing the direct permission call --
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
            force = trigger_hooks("Stop", messages)
            if force is not None:
                messages.append({"role": "user", "content": str(force)})
                continue
            return

        for call in tool_calls:
            name = call.function.name
            arguments = json.loads(call.function.arguments)
            blocked = trigger_hooks("PreToolUse", name, arguments)
            if blocked is not None:
                output = str(blocked)
            else:
                try:
                    handler = TOOL_HANDLERS.get(name)
                    output = handler(**arguments) if handler else f"Error: Unknown tool: {name}"
                except (OSError, ValueError, TypeError) as e:
                    output = f"Error: {e}"
                trigger_hooks("PostToolUse", name, arguments, output)
            print(output[:200])
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": output,
            })

    raise RuntimeError(f"Model call limit reached ({MAX_TURNS})")


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    MODEL = os.environ["MODEL_ID"]
    print("s10: Task System - dependencies and persistent task state (OpenAI Chat Completions)")
    print("Permission rules are a teaching example, not a sandbox.")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    with OpenAI() as client:
        history = [{"role": "system", "content": SYSTEM}]
        while True:
            try:
                query = input("\001\033[36m\002s10 >> \001\033[0m\002")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            trigger_hooks("UserPromptSubmit", query)
            history.append({"role": "user", "content": query})
            agent_loop(history, client, MODEL)
            print(history[-1].get("content") or "")
            print()
