"""Chat backend — workspace management, prompt building, Claude CLI invocation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone


class WorkspaceStore:
    """Manages workspace JSON files in a directory."""

    def __init__(self, base_dir: str = "workspaces"):
        self.base_dir = base_dir

    def list(self) -> list[dict]:
        """Return [{name, path, message_count, last_active}] sorted by last_active desc."""
        os.makedirs(self.base_dir, exist_ok=True)
        result = []
        for fname in os.listdir(self.base_dir):
            if not fname.endswith(".json"):
                continue
            ws = self._read(fname)
            if ws is None:
                continue
            result.append({
                "name": ws.get("name", fname.removesuffix(".json")),
                "path": ws.get("path", ""),
                "message_count": len(ws.get("messages", [])),
                "last_active": ws.get("last_active", ""),
            })
        result.sort(key=lambda w: w["last_active"], reverse=True)
        return result

    @staticmethod
    def _validate_name(name: str) -> None:
        """Raise ValueError if name is not a safe filename component."""
        if not _VALID_WS_NAME_RE.match(name):
            raise ValueError(
                f"Invalid workspace name: '{name}'. "
                "Must be 1-50 alphanumeric/dash/underscore chars, starting with alnum."
            )

    def get(self, name: str) -> dict | None:
        """Return full workspace or None."""
        try:
            self._validate_name(name)
        except ValueError:
            return None
        return self._read(f"{name}.json")

    def create(self, name: str, path: str, config_path: str) -> dict:
        """Create workspace. Raises ValueError on conflict."""
        import yaml

        self._validate_name(name)

        # Check if workspace already exists
        if os.path.isfile(os.path.join(self.base_dir, f"{name}.json")):
            raise ValueError(f"Workspace '{name}' already exists")

        # Check YAML for existing project
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            raw = {}

        projects = raw.get("projects") or {}
        if name in projects:
            yaml_path = projects[name].get("path", "")
            if path and path != yaml_path:
                raise ValueError(
                    f"Project '{name}' already exists with path '{yaml_path}'"
                )
            path = yaml_path  # Use YAML path

        if not path:
            raise ValueError("Path is required")
        if not os.path.isdir(path):
            raise ValueError(f"Path does not exist: {path}")

        ws = {
            "name": name,
            "path": path,
            "session_id": str(uuid.uuid4()),
            "allowed_tools": [],
            "messages": [],
            "created_at": _now_iso(),
            "last_active": _now_iso(),
        }
        self._write(f"{name}.json", ws)
        return ws

    def delete(self, name: str) -> bool:
        """Delete workspace JSON. Returns False if not found."""
        try:
            self._validate_name(name)
        except ValueError:
            return False
        fpath = os.path.join(self.base_dir, f"{name}.json")
        if not os.path.isfile(fpath):
            return False
        os.unlink(fpath)
        return True

    def update_session_id(self, name: str, session_id: str) -> None:
        """Write session_id into workspace JSON (migration helper)."""
        ws = self.get(name)
        if ws is None:
            return
        ws["session_id"] = session_id
        self._write(f"{name}.json", ws)

    def update_allowed_tools(self, name: str, tools: list[str]) -> None:
        """Update the allowed_tools list for a workspace."""
        ws = self.get(name)
        if ws is None:
            return
        ws["allowed_tools"] = tools
        self._write(f"{name}.json", ws)

    def save_system_message(self, name: str, content: str,
                            data: dict | None = None) -> None:
        """Append a system message (e.g. run results) to workspace chat."""
        self._validate_name(name)
        ws = self.get(name)
        if ws is None:
            return
        msgs = ws.setdefault("messages", [])
        msg: dict = {"id": _make_msg_id(), "role": "system",
                      "content": content, "ts": _now_iso()}
        if data:
            msg["data"] = data
        msgs.append(msg)
        ws["last_active"] = _now_iso()
        self._write(f"{name}.json", ws)

    def save_turn(self, name: str, user_msg: str, reply: str,
                  tasks: list[dict]) -> dict | None:
        """Append one turn (user + assistant) to workspace.

        Returns {"user_id": str, "assistant_id": str} or None on failure.
        """
        self._validate_name(name)  # raises on bad name
        ws = self.get(name)
        if ws is None:
            return None
        msgs = ws.setdefault("messages", [])
        user_id = _make_msg_id()
        msgs.append({"id": user_id, "role": "user",
                      "content": user_msg, "ts": _now_iso()})
        assistant_id = _make_msg_id()
        assistant_msg: dict = {"id": assistant_id, "role": "assistant",
                                "content": reply, "ts": _now_iso()}
        if tasks:
            assistant_msg["tasks"] = tasks
        msgs.append(assistant_msg)
        ws["last_active"] = _now_iso()
        self._write(f"{name}.json", ws)
        return {"user_id": user_id, "assistant_id": assistant_id}

    def _read(self, fname: str) -> dict | None:
        fpath = os.path.join(self.base_dir, fname)
        if not os.path.isfile(fpath):
            return None
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, fname: str, data: dict) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        fpath = os.path.join(self.base_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ---- Safety limits ----

_MAX_USER_MESSAGE = 100_000    # argv safety (Linux ~2MB limit)
_MAX_APPEND_SECTION = 4_000    # per-section cap for --append-system-prompt
_MAX_RUN_SUMMARY = 3_000       # per-task result cap in build_run_summary


def _session_exists(session_id: str) -> bool:
    """Check if Claude CLI has a .jsonl session file for this ID."""
    projects_dir = os.path.join(os.path.expanduser("~/.claude"), "projects")
    if not os.path.isdir(projects_dir):
        return False
    for dirpath, _, filenames in os.walk(projects_dir):
        if f"{session_id}.jsonl" in filenames:
            return True
    return False


# ---- Run summary (Rule 2) ----

def build_run_summary(state_dir: str, project_name: str,
                      max_runs_scan: int = 10) -> str:
    """Scan run files, find latest containing project tasks, format summary."""
    if not os.path.isdir(state_dir):
        return ""
    files = [f for f in os.listdir(state_dir) if f.endswith(".json")]
    files.sort(reverse=True)

    # Resolve project root for log file paths
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for run_file in files[:max_runs_scan]:
        try:
            with open(os.path.join(state_dir, run_file), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        tasks = data.get("tasks", {})
        project_tasks = {
            tid: ts for tid, ts in tasks.items()
            if tid.startswith(project_name + ":")
        }
        if not project_tasks:
            continue

        # Format summary — max 5 tasks, each result <= 500 chars
        lines = [f"Run: {data.get('run_id', run_file)}"]
        for i, (tid, ts) in enumerate(project_tasks.items()):
            if i >= 5:
                lines.append(f"  ... and {len(project_tasks) - 5} more tasks")
                break
            status = ts.get("status", "unknown")
            error = ts.get("error", "")
            duration = ts.get("duration_s")
            log_file = ts.get("log_file", "")
            line = f"  {tid}: {status}"
            if duration is not None:
                line += f" ({duration}s)"
            if error:
                line += f" - {error[:500]}"
            elif status == "done" and log_file:
                # Extract result text from log for done tasks
                result_text = _extract_log_result(project_root, log_file)
                if result_text:
                    line += f"\n    Result: {result_text[:500]}"
            lines.append(line)

        result = "\n".join(lines)
        return result[:_MAX_RUN_SUMMARY]

    return ""


def _extract_log_result(project_root: str, log_path: str) -> str:
    """Read a Claude CLI log file and extract the result text."""
    full_path = os.path.join(project_root, log_path)
    if not os.path.isfile(full_path):
        return ""
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(1_048_576)  # 1 MB cap
    except OSError:
        return ""

    # Parse Claude CLI output format
    m = re.search(r"=== STDOUT ===\s*\n(.*?)(?:\n\s*=== STDERR ===|$)",
                  raw, re.DOTALL)
    if not m:
        return ""
    json_str = m.group(1).strip()
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("type") != "result":
        return ""
    return data.get("result", "")


def build_task_list(config_path: str, project_name: str) -> str:
    """Read YAML config and format existing tasks as advisory context.

    Best-effort: returns at most 15 tasks with truncated prompts.
    Used as a hint for the LLM to avoid ID collisions and respect
    dependency structure — not an exhaustive correctness guarantee.
    """
    import yaml

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return ""

    projects = raw.get("projects") or {}
    proj = projects.get(project_name)
    if not proj:
        return ""

    tasks = proj.get("tasks") or []
    lines: list[str] = []
    for task in tasks[:15]:
        tid = task.get("id", "?")
        tool = task.get("tool", "?")
        prompt = task.get("prompt", "")[:100]
        deps = task.get("depends_on") or []
        line = f"- {tid} [{tool}]: {prompt}"
        if deps:
            line += f" (deps: {', '.join(str(d) for d in deps)})"
        lines.append(line)
    return "\n".join(lines)


# ---- Task block parser (Rule 5) ----

_VALID_WS_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")

_TASK_BLOCK_RE = re.compile(
    r"\[TASK\](.*?)\[/TASK\]", re.DOTALL
)
_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")
_VALID_TOOLS = {"claude", "codex", "codex-review"}


def _parse_task_blocks(text: str) -> tuple[str, list[dict]]:
    """Extract [TASK]...[/TASK], syntax-validate, return (clean_text, tasks)."""
    tasks: list[dict] = []
    clean = text

    for match in _TASK_BLOCK_RE.finditer(text):
        block_text = match.group(1).strip()
        task = _parse_single_task(block_text)
        tasks.append(task)

    # Remove all [TASK]...[/TASK] blocks from display text
    clean = _TASK_BLOCK_RE.sub("", clean).strip()
    return clean, tasks


def _parse_single_task(block: str) -> dict:
    """Parse a single task block into a dict with valid/error fields."""
    fields: dict = {
        "id": "",
        "tool": "",
        "prompt": "",
        "depends_on": [],
        "review_of": None,
        "valid": False,
        "error": None,
    }

    for line in block.split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key == "id":
            fields["id"] = value
        elif key == "tool":
            fields["tool"] = value
        elif key == "prompt":
            fields["prompt"] = value
        elif key == "depends_on":
            deps = [d.strip() for d in value.split(",") if d.strip()]
            fields["depends_on"] = deps
        elif key == "review_of":
            fields["review_of"] = value if value else None

    # Validate
    errors: list[str] = []
    if not fields["id"]:
        errors.append("id is required")
    elif not _VALID_ID_RE.match(fields["id"]):
        errors.append(f"invalid id format: {fields['id']}")

    if not fields["tool"]:
        errors.append("tool is required")
    elif fields["tool"] not in _VALID_TOOLS:
        errors.append(f"invalid tool: {fields['tool']}")

    if not fields["prompt"]:
        errors.append("prompt is required")

    if fields["review_of"] and fields["tool"] != "codex-review":
        errors.append("review_of only allowed with tool codex-review")

    if errors:
        fields["error"] = "; ".join(errors)
    else:
        fields["valid"] = True

    return fields


# ---- CLI invocation (Rule 3) ----

def chat_reply(workspace: dict, user_message: str,
               task_list: str, run_summary: str,
               timeout: int = 1200,
               workspace_store: WorkspaceStore | None = None) -> dict:
    """Call Claude CLI with session persistence and return parsed response."""
    session_id = workspace.get("session_id", "")
    ws_name = workspace.get("name", "")
    ws_path = workspace.get("path", "")

    # Migration: old workspace without session_id → generate and persist
    if not session_id:
        session_id = str(uuid.uuid4())
        if workspace_store and ws_name:
            workspace_store.update_session_id(ws_name, session_id)

    user_message = user_message[:_MAX_USER_MESSAGE]
    # CLI argument parser treats leading dashes as options; prepend space to avoid.
    safe_message = " " + user_message if user_message.lstrip().startswith("-") else user_message

    # Build --append-system-prompt: role instruction + ephemeral context.
    # No --system-prompt so Claude CLI preserves its defaults (CLAUDE.md, etc.).
    role_prompt = (
        f"## Task Planning Mode (project: {ws_name})\n"
        "You are also a task planning assistant. When the user asks you to create, "
        "add, or plan a task, you MUST include [TASK]...[/TASK] blocks in your response.\n\n"
        "Format (one block per task):\n"
        "[TASK]\n"
        "id: my-task-id\n"
        "tool: claude\n"
        "prompt: Detailed instruction for the task...\n"
        "depends_on: other-task-id\n"
        "[/TASK]\n\n"
        "To review a file (no depends_on needed):\n"
        "[TASK]\n"
        "id: review-plan\n"
        "tool: codex-review\n"
        "prompt: Review this plan for completeness\n"
        "review_of: .plan/design.md\n"
        "[/TASK]\n\n"
        "Fields:\n"
        "- id: unique identifier (required)\n"
        "- tool: claude | codex | codex-review (required)\n"
        "- prompt: full instruction text (required)\n"
        "- depends_on: comma-separated prerequisite task IDs (optional)\n"
        "- review_of: required for codex-review — use the task ID to review another task's output, "
        "or the actual file path (e.g. '.plan/design.md') to review a file. "
        "Do NOT invent a task ID when the user wants to review a file.\n\n"
        "IMPORTANT: Always output [TASK] blocks when the user wants to create tasks. "
        "Do not just describe the task in prose."
    )
    append_parts: list[str] = [role_prompt]
    if task_list:
        append_parts.append("## Current Tasks\n" + task_list[:_MAX_APPEND_SECTION])
    if run_summary:
        append_parts.append("## Latest Run Results\n" + run_summary[:_MAX_APPEND_SECTION])
    append_prompt = "\n\n".join(append_parts)

    cmd = ["claude", "-p", safe_message, "--output-format", "json"]

    if _session_exists(session_id):
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]

    cmd += ["--append-system-prompt", append_prompt]

    allowed_tools = workspace.get("allowed_tools") or []
    for tool in allowed_tools:
        tool = tool.strip()
        if tool:
            cmd += ["--allowedTools", tool]

    # MCP: pass .mcp.json from workspace if it exists
    if ws_path:
        mcp_config = os.path.join(ws_path, ".mcp.json")
        if os.path.isfile(mcp_config):
            cmd += ["--mcp-config", mcp_config]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env, cwd=ws_path or None,
        )
    except FileNotFoundError as exc:
        if ws_path and not os.path.isdir(ws_path):
            msg = f"Workspace directory not found: {ws_path}"
        else:
            msg = "claude CLI not found"
        return {"ok": False, "reply": "", "tasks": [],
                "error": msg}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reply": "", "tasks": [],
                "error": f"Response timed out ({timeout}s)"}

    if proc.returncode != 0:
        stderr = (proc.stderr or "")[:200]
        return {"ok": False, "reply": "", "tasks": [],
                "error": f"CLI error: {stderr}"}

    # Parse JSON output
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        raw_text = proc.stdout.strip()
        clean, tasks = _parse_task_blocks(raw_text)
        resp = {"ok": True, "reply": clean, "tasks": tasks, "error": None}
        if not tasks and _looks_like_task_request(user_message):
            resp["task_hint"] = "no_tasks_generated"
        return resp

    result_text = data.get("result", "") if isinstance(data, dict) else str(data)
    clean, tasks = _parse_task_blocks(result_text)
    resp: dict = {"ok": True, "reply": clean, "tasks": tasks, "error": None}
    if not tasks and _looks_like_task_request(user_message):
        resp["task_hint"] = "no_tasks_generated"
    return resp


# ---- Task request detection ----

_TASK_REQUEST_KEYWORDS = re.compile(
    r"\b(create|add|plan|generate|make|build|define|write)\b.*\b(task|tasks|step|steps|pipeline)\b",
    re.IGNORECASE,
)


def _looks_like_task_request(message: str) -> bool:
    """Heuristic: does the user message look like a task-creation request?"""
    return bool(_TASK_REQUEST_KEYWORDS.search(message))


# ---- Isolated task generator ----

def generate_tasks(project_name: str, user_message: str,
                   config_path: str, timeout: int = 300) -> dict:
    """Generate tasks in an isolated Claude CLI invocation.

    Unlike chat_reply, this runs without session state, workspace tools,
    or MCP config — purely a stateless prompt-in/tasks-out call.
    Uses a portable temp directory as cwd to avoid touching the workspace.
    """
    user_message = user_message[:_MAX_USER_MESSAGE]
    # CLI argument parser treats leading dashes as options; prepend space to avoid.
    safe_message = " " + user_message if user_message.lstrip().startswith("-") else user_message
    task_list = build_task_list(config_path, project_name)

    system_prompt = (
        "You are a task generator. You MUST output [TASK]...[/TASK] blocks ONLY. "
        "No questions, no prose, no explanation.\n\n"
        "Example:\n"
        "[TASK]\n"
        "id: update-api-docs\n"
        "tool: claude\n"
        "prompt: Update the API documentation to reflect current endpoints\n"
        "[/TASK]\n\n"
        "[TASK]\n"
        "id: review-api-docs\n"
        "tool: codex-review\n"
        "prompt: Review the API documentation update\n"
        "depends_on: update-api-docs\n"
        "review_of: update-api-docs\n"
        "[/TASK]\n\n"
        "[TASK]\n"
        "id: review-design-plan\n"
        "tool: codex-review\n"
        "prompt: Review the design plan for completeness\n"
        "review_of: .plan/design.md\n"
        "[/TASK]\n\n"
        "Fields: id (required, kebab-case), tool: claude|codex|codex-review (required), "
        "prompt (required), depends_on (optional, comma-separated), "
        "review_of (required for codex-review).\n\n"
        "review_of rules:\n"
        "- To review another task's output: review_of = task ID (e.g. 'update-api-docs'), "
        "must also set depends_on.\n"
        "- To review a FILE: review_of = the actual file path from user's message "
        "(e.g. '.plan/design.md', 'docs/plan.md'). Do NOT invent a task ID — use the real path.\n\n"
        f"Project: {project_name}\n"
        "CRITICAL: Output ONLY [TASK]...[/TASK] blocks. Never ask questions."
    )
    if task_list:
        system_prompt += (
            "\n\nExisting tasks (avoid duplicate IDs):\n"
            + task_list[:_MAX_APPEND_SECTION]
        )

    cmd = [
        "claude", "-p", safe_message,
        "--output-format", "json",
        "--model", "sonnet",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env,
            cwd=tempfile.gettempdir(),
        )
    except FileNotFoundError:
        return {"ok": False, "reply": "", "tasks": [],
                "error": "claude CLI not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reply": "", "tasks": [],
                "error": f"Generation timed out ({timeout}s)"}

    if proc.returncode != 0:
        stderr = (proc.stderr or "")[:200]
        return {"ok": False, "reply": "", "tasks": [],
                "error": f"CLI error: {stderr}"}

    try:
        data = json.loads(proc.stdout)
        result_text = data.get("result", "") if isinstance(data, dict) else str(data)
    except (json.JSONDecodeError, ValueError):
        result_text = proc.stdout.strip()

    clean, tasks = _parse_task_blocks(result_text)

    if not tasks:
        return {"ok": False, "reply": clean, "tasks": [],
                "error": "No tasks generated — try a more specific request"}

    return {"ok": True, "reply": clean, "tasks": tasks, "error": None}


# ---- Helpers ----

def _make_msg_id() -> str:
    """Generate a unique message ID."""
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def send_to_whatsapp(message: str) -> dict:
    """Push message to WhatsApp via VPS OpenClaw. Best-effort, never raises.

    NOTE: SSH StrictHostKeyChecking=no is a temporary carryover from notify.py.
    Not a security conclusion — acceptable for PoC against our own VPS.
    """
    import shlex
    safe = re.sub(
        r"[^\w\s.,:;=\-/()+@#\n\u4e00-\u9fff\u3000-\u303f\uff00-\uffef?!]",
        "", message,
    )[:4000]
    inner = f"openclaw message send --channel whatsapp {shlex.quote(safe)}"
    remote = f"su - Nick -c {shlex.quote(inner)}"
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        "root@137.220.43.207", remote,
    ]
    try:
        proc = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
        return {"ok": proc.returncode == 0}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
