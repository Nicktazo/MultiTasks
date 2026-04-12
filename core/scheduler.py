"""Main orchestration loop (Phase 2A: bounded parallel execution)."""

from __future__ import annotations

import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .dag import build_scoped_dag, get_execution_order, should_skip
from .notify import notify_task_failed, notify_pipeline_done
from .runner import capture_git_snapshot, run_task
from .state import RunState, DONE, FAILED

if TYPE_CHECKING:
    from .config import Config, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class TaskExecutionResult:
    task_id: str
    project: str
    status: str        # done | failed | skipped
    error: str = ""


def _resolve_workspace_policy(config: Config, task: Task) -> str:
    """If this task is referenced by a codex-review task's review_of, force "block"."""
    for t in config.tasks.values():
        if t.review_of == task.id:
            return "block"
    return config.settings.dirty_workspace


def resolve_project_scope(config: Config, project: str) -> set[str]:
    """Compute transitive closure of a project's tasks + all upstream dependencies."""
    if project not in config.projects:
        raise ValueError(f"Unknown project: {project}")

    target_ids = {t.id for t in config.projects[project].tasks}
    closure: set[str] = set()
    queue = list(target_ids)
    while queue:
        tid = queue.pop()
        if tid in closure:
            continue
        closure.add(tid)
        task = config.tasks.get(tid)
        if task:
            for dep in task.depends_on:
                queue.append(dep)
    return closure


# ---------------------------------------------------------------------------
# Worker: per-task execution (runs in thread pool)
# ---------------------------------------------------------------------------

def _execute_task(config: Config, state: RunState, task_id: str,
                  project_lock: threading.Lock) -> TaskExecutionResult:
    """Execute a single task. Called from a worker thread.

    Acquires project_lock for the duration of execution.
    Must NOT call dag.done() — only the main thread may advance the DAG.
    """
    task = config.tasks[task_id]
    project = task.project
    cwd = config.projects[project].path

    with project_lock:
        # Git snapshot
        snapshot = capture_git_snapshot(cwd)

        if not snapshot.is_git:
            error = f"Not a git repository: {cwd}"
            state.set_failed(task_id, error, log_file=None)
            print(f"  FAIL  {task_id} (not a git repo: {cwd})")
            notify_task_failed(config.settings, task_id, error)
            return TaskExecutionResult(task_id, project, FAILED, error)

        policy = _resolve_workspace_policy(config, task)

        if snapshot.dirty_files:
            if policy == "block":
                error = f"Dirty workspace blocked ({len(snapshot.dirty_files)} files)"
                state.set_failed(task_id, error, log_file=None)
                print(f"  FAIL  {task_id} (dirty workspace, policy=block)")
                notify_task_failed(config.settings, task_id, error)
                return TaskExecutionResult(task_id, project, FAILED, error)
            elif policy == "warn":
                shown = snapshot.dirty_files[:5]
                print(f"  warn  {task_id}: dirty workspace ({len(snapshot.dirty_files)} files): "
                      f"{[f.strip() for f in shown]}")

        state.set_running(task_id, snapshot.commit, snapshot.dirty_files)

        # Review baseline
        baseline = None
        if task.review_of:
            reviewed_state = state.tasks.get(task.review_of)
            if reviewed_state:
                baseline = reviewed_state.git_baseline
                if reviewed_state.git_dirty:
                    state.set_review_scope(task_id, "possibly_contaminated")
                    print(f"  warn  {task_id}: review scope may be contaminated "
                          f"(reviewed task had {len(reviewed_state.git_dirty)} dirty files)")

        # Execute
        print(f"  start {task_id} (tool={task.tool})")
        result = run_task(
            tool=task.tool,
            prompt=task.prompt,
            cwd=cwd,
            timeout=config.settings.timeout,
            log_dir=config.settings.log_dir,
            run_id=state.run_id,
            task_id=task_id,
            snapshot=snapshot,
            baseline_commit=baseline,
        )

        # Classify result
        if result.exit_code == -3:
            error = result.stderr[:500] or "Review precondition not met (no baseline)"
            state.set_failed(task_id, error, result.log_file)
            print(f"  FAIL  {task_id} (no baseline for review)")
            notify_task_failed(config.settings, task_id, error)
            return TaskExecutionResult(task_id, project, FAILED, error)

        if result.exit_code == -4:
            reason = result.stderr[:500] or "No changes to review"
            state.set_skipped(task_id, reason)
            print(f"  skip  {task_id} ({reason})")
            return TaskExecutionResult(task_id, project, "skipped")

        if result.success:
            state.set_done(task_id, result.exit_code, result.log_file)
            dur = state.tasks[task_id].duration_s
            print(f"  ok    {task_id} ({dur}s)")
            return TaskExecutionResult(task_id, project, "done")

        error = result.stderr[:500] or "Unknown error"
        state.set_failed(task_id, error, result.log_file)
        print(f"  FAIL  {task_id}")
        print(f"        Log: {result.log_file}")
        notify_task_failed(config.settings, task_id, error)
        return TaskExecutionResult(task_id, project, FAILED, error)


def _coerce_future_result(future: Future, config: Config, state: RunState,
                          task_id: str) -> TaskExecutionResult:
    """Get result from a completed future, handling unexpected exceptions."""
    try:
        return future.result()
    except Exception as e:
        # Worker raised unexpectedly — treat as task failure
        project = config.tasks[task_id].project
        error = f"Worker exception: {type(e).__name__}: {str(e)[:400]}"
        state.set_failed(task_id, error, log_file=None)
        print(f"  FAIL  {task_id} (worker exception)")
        notify_task_failed(config.settings, task_id, error)
        return TaskExecutionResult(task_id, project, FAILED, error)


# ---------------------------------------------------------------------------
# Pipeline: main orchestration loop
# ---------------------------------------------------------------------------

def run_pipeline(config: Config, scope: set[str] | None = None,
                 state: RunState | None = None,
                 inherited_done: set[str] | None = None) -> RunState:
    """Execute tasks respecting DAG dependencies with bounded parallelism.

    Args:
        config: Validated config.
        scope: If set, only run tasks in this set (used by run -p).
        state: Pre-built RunState (used by retry). If None, a fresh one is created.
        inherited_done: Task IDs already done from a previous run (used by retry).
            These tasks are not executed but advance the DAG immediately.
    """
    inherited_done = inherited_done or set()

    # Resolve scoped task set
    if scope is not None:
        scoped_ids = scope
    else:
        scoped_ids = set(config.tasks.keys())

    # Build scoped DAG
    dag = build_scoped_dag(config, scoped_ids)

    # Determine order for init_tasks (stable ordering for state file)
    all_order = get_execution_order(config)
    scoped_order = [tid for tid in all_order if tid in scoped_ids]

    if state is None:
        state = RunState(state_dir=config.settings.state_dir)
        state.init_tasks(scoped_order, config=config)

    inherited_count = len(inherited_done)
    pending_count = len(scoped_order) - inherited_count
    print(f"Run: {state.run_id}")
    if inherited_count:
        print(f"Tasks: {len(scoped_order)} ({inherited_count} inherited done, "
              f"{pending_count} to run, max_parallel={config.settings.max_parallel})")
    else:
        print(f"Tasks: {len(scoped_order)} (max_parallel={config.settings.max_parallel})")
    print()

    # Per-project locks and tracking
    projects_in_scope = {config.tasks[tid].project for tid in scoped_ids}
    project_locks: dict[str, threading.Lock] = {p: threading.Lock() for p in projects_in_scope}
    busy_projects: set[str] = set()
    submission_queue: deque[str] = deque()
    running_futures: dict[Future, str] = {}

    max_parallel = config.settings.max_parallel

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        while dag.is_active() or submission_queue or running_futures:
            # 1. Release ready nodes from DAG
            for task_id in dag.get_ready():
                # Inherited done: advance DAG without executing
                if task_id in inherited_done:
                    print(f"  done  {task_id} (inherited)")
                    dag.done(task_id)
                    continue

                reason = should_skip(config, state, task_id)
                if reason:
                    state.set_skipped(task_id, reason)
                    print(f"  skip  {task_id} ({reason})")
                    dag.done(task_id)
                else:
                    submission_queue.append(task_id)

            # 2. Submit from queue respecting capacity + project concurrency
            remaining: deque[str] = deque()
            while submission_queue and len(running_futures) < max_parallel:
                task_id = submission_queue.popleft()
                project = config.tasks[task_id].project
                if project in busy_projects:
                    remaining.append(task_id)
                    continue
                busy_projects.add(project)
                future = executor.submit(
                    _execute_task, config, state, task_id, project_locks[project],
                )
                running_futures[future] = task_id

            # Put back tasks that couldn't be submitted (project busy)
            submission_queue.extendleft(reversed(remaining))

            # 3. Wait for at least one completion
            if running_futures:
                done_futures, _ = wait(running_futures, return_when=FIRST_COMPLETED)
                for future in done_futures:
                    task_id = running_futures.pop(future)
                    result = _coerce_future_result(future, config, state, task_id)
                    busy_projects.discard(result.project)
                    dag.done(task_id)
            elif not submission_queue and not dag.is_active():
                # Nothing running, nothing queued, DAG exhausted — done
                break
            elif submission_queue and not running_futures:
                # Queued tasks but nothing can run — deadlock
                raise RuntimeError(
                    f"Scheduler deadlock: {len(submission_queue)} queued tasks "
                    f"but nothing running. Queued: {list(submission_queue)}"
                )

    # Done
    print()
    print(state.summary())
    notify_pipeline_done(config.settings, state)
    return state
