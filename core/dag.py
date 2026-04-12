"""DAG dependency resolution + upstream status checking."""

from __future__ import annotations

from graphlib import TopologicalSorter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .state import RunState


def build_dag(config: Config) -> TopologicalSorter:
    """Build and prepare() a DAG. CycleError already caught by config validation."""
    graph: dict[str, set[str]] = {}
    for task in config.tasks.values():
        graph[task.id] = set(task.depends_on)
    ts = TopologicalSorter(graph)
    ts.prepare()
    return ts


def build_scoped_dag(config: Config, scope: set[str]) -> TopologicalSorter:
    """Build and prepare() a DAG containing only tasks in scope.

    Raises ValueError if any scoped task has a dependency outside the scope
    (the scope must be a closed upstream set).
    """
    missing: list[str] = []
    graph: dict[str, set[str]] = {}
    for task in config.tasks.values():
        if task.id in scope:
            deps = set(task.depends_on)
            outside = deps - scope
            if outside:
                missing.append(f"  {task.id} depends on {sorted(outside)}")
            graph[task.id] = deps & scope
    if missing:
        raise ValueError(
            "Scope is not closed — these dependencies are outside the scope:\n"
            + "\n".join(missing)
        )
    ts = TopologicalSorter(graph)
    ts.prepare()
    return ts


def get_execution_order(config: Config) -> list[str]:
    """Return serial execution order (static_order). For debugging/validate."""
    graph: dict[str, set[str]] = {}
    for task in config.tasks.values():
        graph[task.id] = set(task.depends_on)
    ts = TopologicalSorter(graph)
    return list(ts.static_order())


def get_dependents(config: Config, task_id: str) -> list[str]:
    """Return direct downstream tasks that depend on task_id."""
    return [
        t.id for t in config.tasks.values()
        if task_id in t.depends_on
    ]


def should_skip(config: Config, state: RunState, task_id: str) -> str | None:
    """Check if a task should be skipped due to upstream failures.

    Examines all upstream dependencies (depends_on) in state.
    If any upstream is failed or skipped, returns a skip reason string.
    If all upstreams are done, returns None (task can execute).

    This is the single entry point for failure propagation.
    The scheduler must call this before executing any task released by the DAG.
    """
    task = config.tasks[task_id]
    for dep_id in task.depends_on:
        dep_state = state.tasks.get(dep_id)
        if dep_state is None:
            return f"upstream '{dep_id}' not in state"
        if dep_state.status == "failed":
            return f"upstream '{dep_id}' failed"
        if dep_state.status == "skipped":
            return f"upstream '{dep_id}' was skipped"
    return None
