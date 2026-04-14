"""Chat backend — workspace management, prompt building, Claude CLI invocation."""

from __future__ import annotations

import json
import os
import re
import subprocess
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

    def save_turn(self, name: str, user_msg: str, reply: str,
                  tasks: list[dict]) -> None:
        """Append one turn (user + assistant) to workspace."""
        self._validate_name(name)  # raises on bad name
        ws = self.get(name)
        if ws is None:
            return
        msgs = ws.setdefault("messages", [])
        msgs.append({"role": "user", "content": user_msg, "ts": _now_iso()})
        assistant_msg: dict = {"role": "assistant", "content": reply, "ts": _now_iso()}
        if tasks:
            assistant_msg["tasks"] = tasks
        msgs.append(assistant_msg)
        ws["last_active"] = _now_iso()
        self._write(f"{name}.json", ws)

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


# ---- Prompt building (Rule 4) ----

_CAPS = {
    "system": 600,
    "tasks": 1500,
    "results": 3000,
    "history": 6000,
    "message": 2000,
}


def _truncate(text: str, limit: int, note: str = "") -> str:
    if len(text) <= limit:
        return text
    suffix = f"\n[{note}]" if note else ""
    return text[:limit] + suffix


def _build_prompt(workspace: dict, user_message: str,
                  task_list: str, run_summary: str) -> str:
    """Assemble prompt. Each section independently capped."""
    name = workspace.get("name", "")
    path = workspace.get("path", "")

    system = _truncate(
        f"You are a task planning assistant for project '{name}' at {path}. "
        f"Help the user plan, create, and iterate on tasks. "
        f"To suggest a task, use [TASK]...[/TASK] blocks with fields: "
        f"id, tool (claude|codex|codex-review), prompt, depends_on (optional), review_of (optional).",
        _CAPS["system"],
    )

    tasks_section = ""
    if task_list:
        tasks_section = "\n\n## Current Tasks\n" + _truncate(
            task_list, _CAPS["tasks"], "task list truncated"
        )

    results_section = ""
    if run_summary:
        results_section = "\n\n## Latest Run Results\n" + _truncate(
            run_summary, _CAPS["results"], "results truncated"
        )

    # Build history from messages (newest first, stop at cap)
    messages = workspace.get("messages", [])
    history_parts: list[str] = []
    history_chars = 0
    for msg in reversed(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        line = f"{role}: {content}"
        if history_chars + len(line) > _CAPS["history"]:
            break
        history_parts.insert(0, line)
        history_chars += len(line)
        if len(history_parts) >= 20:
            break

    history_section = ""
    if history_parts:
        history_section = "\n\n## Chat History\n" + "\n".join(history_parts)

    user_section = "\n\nUser: " + _truncate(
        user_message, _CAPS["message"], "message truncated"
    )

    return system + tasks_section + results_section + history_section + user_section


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
        return result[:_CAPS["results"]]

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
    """Read YAML config and format tasks for the given project."""
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
               timeout: int = 120) -> dict:
    """Call Claude CLI and return parsed response."""
    prompt = _build_prompt(workspace, user_message, task_list, run_summary)

    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    env = os.environ.copy()

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except FileNotFoundError:
        return {"ok": False, "reply": "", "tasks": [],
                "error": "claude CLI not found"}
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
        # Fallback: treat stdout as plain text
        raw_text = proc.stdout.strip()
        clean, tasks = _parse_task_blocks(raw_text)
        return {"ok": True, "reply": clean, "tasks": tasks, "error": None}

    result_text = data.get("result", "") if isinstance(data, dict) else str(data)
    clean, tasks = _parse_task_blocks(result_text)
    return {"ok": True, "reply": clean, "tasks": tasks, "error": None}


# ---- Helpers ----

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
