"""YAML config loading + complete validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from graphlib import TopologicalSorter, CycleError

import yaml


class ConfigError(Exception):
    pass


@dataclass
class Task:
    id: str                          # global unique ID (project:task_id)
    local_id: str                    # project-local ID
    project: str                     # owning project name
    prompt: str                      # prompt sent to claude/codex
    tool: str                        # "claude" | "codex" | "codex-review"
    depends_on: list[str] = field(default_factory=list)  # global IDs
    review_of: str | None = None     # codex-review: which task to review (global ID)
    done: bool = False               # marked complete — skipped by pipeline


@dataclass
class Project:
    name: str
    path: str                        # absolute path
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Settings:
    max_parallel: int = 2
    notify: str = "none"
    dashboard_port: int = 8704
    timeout: int = 600
    state_dir: str = "state"
    log_dir: str = "logs"
    dirty_workspace: str = "warn"    # warn | block | ignore
    public_base_url: str = ""        # e.g. "https://mt.imagecolor.cn"
    listen_address: str = "127.0.0.1"  # "0.0.0.0" to accept external traffic
    mobile_mode: bool = False        # push WhatsApp reply links on chat


@dataclass
class Config:
    projects: dict[str, Project] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)


VALID_TOOLS = ("claude", "codex", "codex-review")
VALID_NOTIFY = ("whatsapp", "telegram", "none")
VALID_DIRTY = ("warn", "block", "ignore")

_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")


def _is_task_id(s: str) -> bool:
    """True if s looks like a single task ID (local or global)."""
    if _TASK_ID_RE.match(s):
        return True
    if ":" in s:
        proj, _, tid = s.partition(":")
        if _TASK_ID_RE.match(proj) and _TASK_ID_RE.match(tid):
            return True
    return False


def _is_file_review(review_of: str) -> bool:
    """True if review_of is a file path rather than a task ID reference.

    Supports comma-separated task IDs (e.g. 'task-a,task-b,task-c').
    Returns False for comma-separated values (even malformed ones like
    'a,,b') so they go through task-ID validation and get proper errors.
    """
    if "," in review_of:
        return False  # comma-separated → always task-ID path
    return not _is_task_id(review_of)


def _transitive_deps(task_id: str, tasks: dict[str, Task]) -> set[str]:
    """Return the transitive closure of depends_on for a task."""
    visited: set[str] = set()
    stack = list(tasks[task_id].depends_on) if task_id in tasks else []
    while stack:
        dep = stack.pop()
        if dep in visited:
            continue
        visited.add(dep)
        if dep in tasks:
            stack.extend(tasks[dep].depends_on)
    return visited


def load_config(path: str = "projects.yaml") -> Config:
    """Load and fully validate a projects.yaml config file."""
    errors: list[str] = []

    # --- Parse YAML ---
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error: {e}")

    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping")

    # --- Projects ---
    raw_projects = raw.get("projects")
    if raw_projects is None:
        raw_projects = {}
    if not isinstance(raw_projects, dict):
        raise ConfigError("'projects' must be a mapping")

    # --- Settings ---
    raw_settings = raw.get("settings", {}) or {}
    settings = Settings()

    mp = raw_settings.get("max_parallel", settings.max_parallel)
    if not isinstance(mp, int) or mp < 1:
        errors.append(f"settings.max_parallel must be int >= 1, got {mp!r}")
    else:
        settings.max_parallel = mp

    to = raw_settings.get("timeout", settings.timeout)
    if not isinstance(to, int) or to <= 0:
        errors.append(f"settings.timeout must be int > 0, got {to!r}")
    else:
        settings.timeout = to

    nt = raw_settings.get("notify", settings.notify)
    if nt not in VALID_NOTIFY:
        errors.append(f"settings.notify must be one of {VALID_NOTIFY}, got {nt!r}")
    else:
        settings.notify = nt

    dp = raw_settings.get("dashboard_port", settings.dashboard_port)
    if not isinstance(dp, int) or not (1024 <= dp <= 65535):
        errors.append(f"settings.dashboard_port must be int 1024-65535, got {dp!r}")
    else:
        settings.dashboard_port = dp

    dw = raw_settings.get("dirty_workspace", settings.dirty_workspace)
    if dw not in VALID_DIRTY:
        errors.append(f"settings.dirty_workspace must be one of {VALID_DIRTY}, got {dw!r}")
    else:
        settings.dirty_workspace = dw

    settings.state_dir = raw_settings.get("state_dir", settings.state_dir)
    settings.log_dir = raw_settings.get("log_dir", settings.log_dir)

    pbu = raw_settings.get("public_base_url", settings.public_base_url)
    if not isinstance(pbu, str):
        errors.append(f"settings.public_base_url must be a string, got {pbu!r}")
    else:
        settings.public_base_url = pbu

    la = raw_settings.get("listen_address", settings.listen_address)
    valid_addresses = ("127.0.0.1", "0.0.0.0")
    if la not in valid_addresses:
        errors.append(f"settings.listen_address must be one of {valid_addresses}, got {la!r}")
    else:
        settings.listen_address = la

    mm = raw_settings.get("mobile_mode", settings.mobile_mode)
    if not isinstance(mm, bool):
        errors.append(f"settings.mobile_mode must be a boolean, got {mm!r}")
    else:
        settings.mobile_mode = mm

    # --- Build projects and tasks ---
    config = Config(settings=settings)
    global_ids: set[str] = set()

    for proj_name, proj_data in raw_projects.items():
        if not isinstance(proj_data, dict):
            errors.append(f"Project '{proj_name}' must be a mapping")
            continue

        proj_path = proj_data.get("path", "")
        if not proj_path or not os.path.isdir(proj_path):
            errors.append(f"Project '{proj_name}': path '{proj_path}' does not exist or is not a directory")

        raw_tasks = proj_data.get("tasks", [])
        if not isinstance(raw_tasks, list) or not raw_tasks:
            errors.append(f"Project '{proj_name}': tasks must be a non-empty list")
            continue

        project = Project(name=proj_name, path=proj_path)
        local_ids: set[str] = set()

        for i, rt in enumerate(raw_tasks):
            if not isinstance(rt, dict):
                errors.append(f"Project '{proj_name}', task #{i}: must be a mapping")
                continue

            local_id = rt.get("id")
            prompt = rt.get("prompt")
            tool = rt.get("tool")

            if not local_id:
                errors.append(f"Project '{proj_name}', task #{i}: 'id' is required")
                continue
            if not prompt:
                errors.append(f"Project '{proj_name}', task '{local_id}': 'prompt' is required")
            if not tool:
                errors.append(f"Project '{proj_name}', task '{local_id}': 'tool' is required")
            elif tool not in VALID_TOOLS:
                errors.append(f"Project '{proj_name}', task '{local_id}': tool must be one of {VALID_TOOLS}, got {tool!r}")

            if local_id in local_ids:
                errors.append(f"Project '{proj_name}', task '{local_id}': duplicate local ID")
            local_ids.add(local_id)

            global_id = f"{proj_name}:{local_id}"
            if global_id in global_ids:
                errors.append(f"Task '{global_id}': duplicate global ID")
            global_ids.add(global_id)

            # Normalize depends_on to global IDs
            raw_deps = rt.get("depends_on", []) or []
            deps: list[str] = []
            for dep in raw_deps:
                if ":" in dep:
                    deps.append(dep)
                else:
                    deps.append(f"{proj_name}:{dep}")

            # review_of — single task ID, comma-separated IDs, or file path
            review_of_raw = rt.get("review_of")
            review_of = None
            if review_of_raw:
                if _is_file_review(review_of_raw):
                    review_of = review_of_raw
                else:
                    # Qualify each task ID with project prefix
                    parts = [p.strip() for p in review_of_raw.split(",")]
                    qualified = []
                    for p in parts:
                        if not p:
                            errors.append(f"Task '{global_id}': review_of contains empty entry")
                        elif ":" in p:
                            qualified.append(p)
                        else:
                            qualified.append(f"{proj_name}:{p}")
                    review_of = ",".join(qualified) if qualified else None

            if tool == "codex-review" and not review_of:
                errors.append(f"Task '{global_id}': codex-review tasks must have 'review_of'")

            raw_done = rt.get("done", False)
            done = raw_done is True  # strict: only bool True, not truthy strings

            task = Task(
                id=global_id,
                local_id=local_id,
                project=proj_name,
                prompt=prompt or "",
                tool=tool or "",
                depends_on=deps,
                review_of=review_of,
                done=done,
            )
            project.tasks.append(task)
            config.tasks[global_id] = task

        config.projects[proj_name] = project

    # --- Validate references ---
    for task in config.tasks.values():
        for dep in task.depends_on:
            if dep not in global_ids:
                errors.append(f"Task '{task.id}': depends_on '{dep}' does not exist")

        if task.review_of:
            if _is_file_review(task.review_of):
                pass  # File path: no existence/same-project/depends_on checks
            else:
                review_ids = [r.strip() for r in task.review_of.split(",")]
                for rid in review_ids:
                    if rid not in global_ids:
                        errors.append(f"Task '{task.id}': review_of '{rid}' does not exist")
                    else:
                        if rid in config.tasks:
                            reviewed = config.tasks[rid]
                            if reviewed.project != task.project:
                                errors.append(f"Task '{task.id}': review_of '{rid}' must be in the same project")
                # All review_of targets must be upstream (transitively reachable via depends_on)
                upstream = _transitive_deps(task.id, config.tasks)
                for rid in review_ids:
                    if rid in global_ids and rid not in upstream:
                        errors.append(f"Task '{task.id}': review_of '{rid}' is not an upstream dependency")

    # --- DAG cycle check ---
    try:
        dag: dict[str, set[str]] = {}
        for task in config.tasks.values():
            dag[task.id] = set(task.depends_on)
        ts = TopologicalSorter(dag)
        ts.prepare()
    except CycleError as e:
        errors.append(f"DAG has a cycle: {e}")

    # --- State/log dir writability ---
    for dirname, label in [(settings.state_dir, "state_dir"), (settings.log_dir, "log_dir")]:
        try:
            os.makedirs(dirname, exist_ok=True)
        except OSError as e:
            errors.append(f"{label} '{dirname}' cannot be created: {e}")

    # --- Report all errors ---
    if errors:
        raise ConfigError("Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    return config
