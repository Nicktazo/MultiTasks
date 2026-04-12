"""Config serialization, atomic writes, and mutation-with-validation."""

from __future__ import annotations

import os
import tempfile
import threading
from typing import Any, Callable

import yaml

from .config import load_config, ConfigError, Settings

_config_lock = threading.Lock()

# Fixed key order for settings serialization
_SETTINGS_KEYS = [
    "max_parallel", "timeout", "notify", "dashboard_port",
    "state_dir", "log_dir", "dirty_workspace",
]


def config_to_dict(config) -> dict:
    """Convert a Config dataclass back to a YAML-serializable dict.

    Stability rules:
    - Projects: preserve insertion order
    - Tasks within a project: preserve list order
    - depends_on: local ID for same-project, global ID for cross-project
    - review_of: same rule as depends_on
    - Settings: fixed key order
    """
    projects = {}
    for proj_name, proj in config.projects.items():
        tasks = []
        for task in proj.tasks:
            t: dict[str, Any] = {"id": task.local_id, "prompt": task.prompt, "tool": task.tool}
            if task.depends_on:
                deps = []
                for dep in task.depends_on:
                    if ":" in dep:
                        dep_proj, dep_id = dep.split(":", 1)
                        deps.append(dep_id if dep_proj == proj_name else dep)
                    else:
                        deps.append(dep)
                t["depends_on"] = deps
            if task.review_of:
                if ":" in task.review_of:
                    ro_proj, ro_id = task.review_of.split(":", 1)
                    t["review_of"] = ro_id if ro_proj == proj_name else task.review_of
                else:
                    t["review_of"] = task.review_of
            tasks.append(t)
        projects[proj_name] = {"path": proj.path, "tasks": tasks}

    settings = {}
    defaults = Settings()
    s = config.settings
    for key in _SETTINGS_KEYS:
        settings[key] = getattr(s, key)

    return {"projects": projects, "settings": settings}


def apply_mutation(
    config_path: str,
    mutate_fn: Callable[[dict, dict], list[str]],
    args: dict,
) -> dict:
    """Single entry point for all config changes.

    Pattern: lock -> read YAML -> apply mutate_fn -> write temp -> validate -> atomic replace.
    Returns {"ok": True, "config": raw_dict} or {"ok": False, "errors": [...]}.
    """
    with _config_lock:
        # Read current YAML
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            raw = {"projects": {}, "settings": {}}
        except yaml.YAMLError as e:
            return {"ok": False, "errors": [f"YAML parse error: {e}"]}

        if not isinstance(raw, dict):
            raw = {"projects": {}, "settings": {}}

        # Apply mutation
        errors = mutate_fn(raw, args)
        if errors:
            return {"ok": False, "errors": errors}

        # Write to temp file
        parent_dir = os.path.dirname(os.path.abspath(config_path))
        fd, tmp_path = tempfile.mkstemp(dir=parent_dir, suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            # Validate by loading the temp file
            try:
                load_config(tmp_path)
            except ConfigError as e:
                os.unlink(tmp_path)
                return {"ok": False, "errors": [str(e)]}

            # Atomic replace
            os.replace(tmp_path, config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return {"ok": True, "config": raw}


# ---------------------------------------------------------------------------
# Mutation functions — each takes (raw_dict, args), mutates in-place,
# returns list of error strings (empty = success)
# ---------------------------------------------------------------------------

def _mutate_update_project(raw: dict, args: dict) -> list[str]:
    """Update path of an existing project. Cannot create."""
    name = args.get("name", "")
    path = args.get("path", "")
    projects = raw.get("projects", {})
    if not isinstance(projects, dict) or name not in projects:
        return [f"Project '{name}' does not exist"]
    projects[name]["path"] = path
    return []


def _mutate_delete_project(raw: dict, args: dict) -> list[str]:
    """Delete a project if no external references point to it."""
    name = args.get("name", "")
    projects = raw.get("projects", {})
    if not isinstance(projects, dict) or name not in projects:
        return [f"Project '{name}' does not exist"]

    # Cannot delete the last project
    if len(projects) <= 1:
        return ["Cannot delete the last project"]

    # Check for external references
    refs = _find_project_refs(projects, name)
    if refs:
        return [f"Cannot delete '{name}': {', '.join(refs)}"]

    del projects[name]
    return []


def _mutate_upsert_task(raw: dict, args: dict) -> list[str]:
    """Add or update a task. Creates project if it doesn't exist (requires project_path)."""
    project = args.get("project", "")
    project_path = args.get("project_path", "")
    task_id = args.get("id", "")
    prompt = args.get("prompt", "")
    tool = args.get("tool", "")
    depends_on = args.get("depends_on", []) or []
    review_of = args.get("review_of", "") or ""

    if not project or not task_id:
        return ["'project' and 'id' are required"]

    projects = raw.setdefault("projects", {})

    # Create project if new
    if project not in projects:
        if not project_path:
            return [f"Project '{project}' does not exist; 'project_path' is required to create it"]
        projects[project] = {"path": project_path, "tasks": []}

    proj = projects[project]
    tasks = proj.setdefault("tasks", [])

    # Build task dict
    task_data: dict[str, Any] = {"id": task_id, "prompt": prompt, "tool": tool}
    if depends_on:
        task_data["depends_on"] = depends_on
    if review_of:
        task_data["review_of"] = review_of

    # Update existing or append
    for i, t in enumerate(tasks):
        if isinstance(t, dict) and t.get("id") == task_id:
            tasks[i] = task_data
            return []
    tasks.append(task_data)
    return []


def _mutate_delete_task(raw: dict, args: dict) -> list[str]:
    """Delete a task if no other task references it."""
    project = args.get("project", "")
    task_id = args.get("id", "")
    projects = raw.get("projects", {})

    if project not in projects:
        return [f"Project '{project}' does not exist"]

    tasks = projects[project].get("tasks", [])
    found = False
    for i, t in enumerate(tasks):
        if isinstance(t, dict) and t.get("id") == task_id:
            found = True
            break
    if not found:
        return [f"Task '{task_id}' not found in project '{project}'"]

    # Check references from other tasks
    global_id = f"{project}:{task_id}"
    refs = _find_task_refs(projects, project, task_id)
    if refs:
        return [f"Cannot delete '{global_id}': referenced by {', '.join(refs)}"]

    # Cannot delete the last task in the last project... actually, we can't
    # leave a project with zero tasks. Check:
    if len(tasks) <= 1:
        # This would leave the project with no tasks — delete the project too,
        # but only if it's not the last project
        if len(projects) <= 1:
            return ["Cannot delete the last task in the last project"]
        # Delete the whole project
        del projects[project]
    else:
        tasks.pop(i)

    return []


def _mutate_settings(raw: dict, args: dict) -> list[str]:
    """Update settings fields."""
    settings = raw.setdefault("settings", {})
    for key, value in args.items():
        if key in _SETTINGS_KEYS:
            settings[key] = value
    return []


# ---------------------------------------------------------------------------
# Reference-checking helpers
# ---------------------------------------------------------------------------

def _resolve_global_id(project: str, ref: str) -> str:
    """Resolve a possibly-local task ID to a global one."""
    if ":" in ref:
        return ref
    return f"{project}:{ref}"


def _find_task_refs(projects: dict, target_project: str, target_id: str) -> list[str]:
    """Find all tasks that reference the given task via depends_on or review_of.

    Only returns references from *other* tasks (not the target itself).
    """
    global_target = f"{target_project}:{target_id}"
    refs = []
    for proj_name, proj_data in projects.items():
        if not isinstance(proj_data, dict):
            continue
        for t in proj_data.get("tasks", []):
            if not isinstance(t, dict):
                continue
            tid = t.get("id", "")
            task_global = f"{proj_name}:{tid}"
            if task_global == global_target:
                continue
            # Check depends_on
            for dep in (t.get("depends_on") or []):
                if _resolve_global_id(proj_name, dep) == global_target:
                    refs.append(f"{task_global} (depends_on)")
                    break
            # Check review_of
            ro = t.get("review_of")
            if ro and _resolve_global_id(proj_name, ro) == global_target:
                refs.append(f"{task_global} (review_of)")
    return refs


def _find_project_refs(projects: dict, target_project: str) -> list[str]:
    """Find all tasks outside target_project that reference any task in it."""
    # Collect all task IDs in the target project
    proj_data = projects.get(target_project, {})
    target_ids = set()
    for t in (proj_data.get("tasks") or []):
        if isinstance(t, dict) and t.get("id"):
            target_ids.add(f"{target_project}:{t['id']}")

    refs = []
    for proj_name, pd in projects.items():
        if proj_name == target_project or not isinstance(pd, dict):
            continue
        for t in pd.get("tasks", []):
            if not isinstance(t, dict):
                continue
            tid = f"{proj_name}:{t.get('id', '')}"
            for dep in (t.get("depends_on") or []):
                if _resolve_global_id(proj_name, dep) in target_ids:
                    refs.append(f"{tid} (depends_on)")
                    break
            ro = t.get("review_of")
            if ro and _resolve_global_id(proj_name, ro) in target_ids:
                refs.append(f"{tid} (review_of)")
    return refs
