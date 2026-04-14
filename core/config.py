"""YAML config loading + complete validation."""

from __future__ import annotations

import os
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


@dataclass
class Project:
    name: str
    path: str                        # absolute path
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Settings:
    max_parallel: int = 2
    notify: str = "none"
    dashboard_port: int = 18300
    timeout: int = 600
    state_dir: str = "state"
    log_dir: str = "logs"
    dirty_workspace: str = "warn"    # warn | block | ignore


@dataclass
class Config:
    projects: dict[str, Project] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)


VALID_TOOLS = ("claude", "codex", "codex-review")
VALID_NOTIFY = ("whatsapp", "telegram", "none")
VALID_DIRTY = ("warn", "block", "ignore")


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

            # review_of
            review_of_raw = rt.get("review_of")
            review_of = None
            if review_of_raw:
                if ":" in review_of_raw:
                    review_of = review_of_raw
                else:
                    review_of = f"{proj_name}:{review_of_raw}"

            if tool == "codex-review" and not review_of:
                errors.append(f"Task '{global_id}': codex-review tasks must have 'review_of'")

            task = Task(
                id=global_id,
                local_id=local_id,
                project=proj_name,
                prompt=prompt or "",
                tool=tool or "",
                depends_on=deps,
                review_of=review_of,
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
            if task.review_of not in global_ids:
                errors.append(f"Task '{task.id}': review_of '{task.review_of}' does not exist")
            else:
                if task.review_of in config.tasks:
                    reviewed = config.tasks[task.review_of]
                    if reviewed.project != task.project:
                        errors.append(f"Task '{task.id}': review_of '{task.review_of}' must be in the same project")
                if task.review_of not in task.depends_on:
                    errors.append(f"Task '{task.id}': review_of '{task.review_of}' must also appear in depends_on")

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
