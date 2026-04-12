# Phase 2A Spec: Parallel Scheduler

This document defines the Phase 2A upgrade from the current serial scheduler to bounded parallel execution.

Scope:

- Add parallel task execution with `ThreadPoolExecutor`
- Preserve current Phase 1 behavior for validation, failure propagation, logs, and notifications
- Do not implement resume/retry yet
- Do not implement dashboard live updates yet

Out of scope:

- Resume / retry
- Dashboard SSE listeners
- Cross-run state inheritance
- Any config format change

## Goals

Phase 2A should improve throughput across independent projects while keeping task outcomes deterministic.

Required properties:

- Respect DAG dependencies
- Respect `settings.max_parallel`
- Never run two tasks from the same project at the same time
- Keep `should_skip()` as the only failure-propagation gate
- Preserve atomic state persistence
- Preserve per-task log files
- Preserve existing exit behavior:
  - task `failed` => pipeline exit code `1`
  - task `skipped` does not fail the pipeline by itself

## Non-Goals

Phase 2A is not trying to maximize scheduler cleverness.

Specifically, it will not:

- reorder work to optimize for longest path
- retry failed tasks automatically
- interrupt running tasks
- isolate tasks in separate git worktrees

## Current Baseline

Phase 1 behavior is:

1. Build a topological order from the config
2. Iterate tasks serially
3. Before each task, call `should_skip()`
4. Capture git snapshot
5. Enforce workspace policy
6. Run tool
7. Persist final state and notify on failure

Phase 2A keeps the same per-task behavior but changes the orchestration loop from serial iteration to event-driven scheduling.

## Design Summary

The scheduler will:

1. Build and `prepare()` a `TopologicalSorter`
2. Pull ready tasks from `dag.get_ready()`
3. For each ready task:
   - call `should_skip()`
   - if skipped, mark skipped and call `dag.done(task_id)`
   - otherwise place it into a pending-ready queue
4. Submit pending-ready tasks to a `ThreadPoolExecutor` when:
   - total running tasks is below `settings.max_parallel`
   - the task's project is not currently busy
5. As futures complete:
   - update `RunState`
   - notify on failure
   - mark the project idle
   - call `dag.done(task_id)`
6. Loop until:
   - no DAG nodes remain active
   - no tasks are queued for submission
   - no futures are still running

The DAG still models only dependency completion, not success. Business-level skip behavior continues to live in `should_skip()`.

## Scheduler Architecture

### New Internal Concepts

`project_locks: dict[str, threading.Lock]`

- One lock per project
- A worker must hold the project's lock during task execution
- This guarantees one active task per project even if multiple same-project tasks become ready together

`running_futures: dict[Future, str]`

- Maps executor futures to `task_id`
- Used to recover which task completed

`busy_projects: set[str]`

- Tracks projects that already have an in-flight task
- Avoids oversubmitting same-project tasks that would only block on a lock

`submission_queue: deque[str]`

- Holds ready-to-run tasks that passed `should_skip()` but could not yet be submitted
- Reasons for delay:
  - executor capacity full
  - project already busy

### Why both `busy_projects` and `project_locks`

The lock is the correctness guard.

The busy set is the scheduling guard.

Without `busy_projects`, the executor could fill with tasks from one project that then block on the same lock, starving other projects. The scheduler must avoid that by only submitting one task per project at a time.

## Public Function Shape

Phase 2A keeps the public entry point:

```python
def run_pipeline(config: Config, scope: set[str] | None = None) -> RunState:
```

No CLI or config change is required for this phase.

Behavior change:

- If `settings.max_parallel == 1`, execution should behave like Phase 1
- If `settings.max_parallel > 1`, independent tasks may overlap

## Worker Boundary

Extract Phase 1's per-task logic into one helper:

```python
def _execute_task(config: Config, state: RunState, task_id: str) -> TaskExecutionResult:
```

`TaskExecutionResult` should be a small internal dataclass:

```python
@dataclass
class TaskExecutionResult:
    task_id: str
    project: str
    status: str        # done | failed | skipped
    error: str = ""
```

Rules:

- The worker performs the exact same task-level behavior as Phase 1
- The worker is responsible for:
  - git snapshot
  - workspace policy enforcement
  - `state.set_running(...)`
  - `run_task(...)`
  - final `state.set_done/failed/skipped(...)`
  - failure notification
- The outer scheduler is responsible for:
  - DAG progression
  - respecting executor capacity
  - respecting per-project concurrency
  - deciding when a task may start

This split keeps state changes close to task execution and keeps the orchestration loop readable.

## Detailed Execution Flow

### 1. Setup

At the start of `run_pipeline(...)`:

1. Resolve `scope`
2. Build a scoped task set
3. Build a scoped DAG
4. Initialize `RunState` with only scoped tasks
5. Create:
   - `ThreadPoolExecutor(max_workers=config.settings.max_parallel)`
   - `project_locks`
   - `busy_projects`
   - `submission_queue`
   - `running_futures`

Important: the DAG must only contain tasks inside `scope`. Dependencies outside scope should never exist because `resolve_project_scope()` already includes upstream closure.

### 2. Releasing Ready Nodes

Whenever the scheduler polls the DAG:

```python
for task_id in dag.get_ready():
    reason = should_skip(config, state, task_id)
    if reason:
        state.set_skipped(task_id, reason)
        print(...)
        dag.done(task_id)
    else:
        submission_queue.append(task_id)
```

This remains the only entry point for failure propagation.

No recursive downstream skipping is added.

### 3. Submitting Work

While there is queue capacity:

- pop the next task from `submission_queue`
- inspect its project
- if that project is already in `busy_projects`, push the task back to the queue tail
- otherwise:
  - mark project busy
  - submit a worker future
  - store `running_futures[future] = task_id`

Queue rule:

- FIFO is sufficient
- no priority scheduler is required for Phase 2A

### 4. Worker Execution

Inside the worker:

1. Acquire the project's lock
2. Re-run no DAG logic
3. Run the same task steps as Phase 1
4. Return a `TaskExecutionResult`

The worker must not call `dag.done(...)`.

Only the main scheduler thread may advance the DAG.

### 5. Completion Handling

Use `concurrent.futures.wait(..., return_when=FIRST_COMPLETED)` against the current future set.

For each completed future:

- recover `task_id`
- call `future.result()`
- remove the future from `running_futures`
- remove the project from `busy_projects`
- call `dag.done(task_id)`

Failure handling:

- If the worker raises unexpectedly, treat that task as failed
- Persist the failure into `RunState`
- Send failure notification
- Still call `dag.done(task_id)` so downstream nodes can later be skipped via `should_skip()`

## State Model

No new persisted status values are introduced in Phase 2A.

Allowed final statuses remain:

- `done`
- `failed`
- `skipped`

Intermediate status remains:

- `running`

### Thread Safety Rules

`RunState` already serializes writes with a lock. Phase 2A relies on that.

Additional rules:

- only `RunState` methods may mutate persisted task fields
- scheduler code must not directly write `state.tasks[task_id].field = ...`
- if a new state field is needed later, add a setter method instead of in-place mutation

This matters because there is already one direct mutation in current code for `review_scope`; that should be converted into a setter before or during Phase 2A.

Recommended addition:

```python
def set_review_scope(self, task_id: str, review_scope: str) -> None:
```

## Project Concurrency Rules

Hard rule:

- At most one running task per project

This rule applies even if:

- the tasks are independent in the DAG
- `max_parallel` is larger than the number of projects

Rationale:

- avoids concurrent git mutations
- avoids CLI tool interference inside the same repo

## Failure Semantics

Phase 2A keeps Phase 1 semantics exactly:

- `not a git repo` => `failed`
- dirty workspace with `block` => `failed`
- codex-review with no baseline => `failed`
- codex-review with no diff => `skipped`
- any nonzero tool exit except special review cases => `failed`

The parallel scheduler must not add any new failure categories.

## Notification Semantics

No change in Phase 2A:

- send immediate notification for each failed task
- send one pipeline summary at the end
- do not notify on success
- do not notify on skipped

Important:

- each failed task must notify once, even in unexpected worker exceptions

## Console Output

Output order will become partially nondeterministic once tasks run in parallel.

That is acceptable, but the format should stay consistent:

- main thread prints task submission / skip events
- worker-completion handling prints final `ok` / `FAIL` / `skip`

Do not attempt to fully serialize pretty logging in Phase 2A.

Minimal requirement:

- each line should remain single-line and self-contained
- always include `task_id`

## Suggested Implementation Steps

1. Introduce `_execute_task(...)` and move current per-task logic into it
2. Add `RunState.set_review_scope(...)`
3. Add a helper to build a scoped DAG
4. Replace the serial `for task_id in order` loop with the event loop
5. Keep `max_parallel == 1` behavior working
6. Verify failure notifications still fire once

## Pseudocode

```python
def run_pipeline(config, scope=None):
    scoped_ids = _resolve_scoped_ids(config, scope)
    dag = build_scoped_dag(config, scoped_ids)
    state = RunState(state_dir=config.settings.state_dir)
    state.init_tasks(sorted_scoped_order)

    project_locks = {name: threading.Lock() for name in _projects_in_scope(...)}
    busy_projects = set()
    submission_queue = deque()
    running_futures = {}

    with ThreadPoolExecutor(max_workers=config.settings.max_parallel) as executor:
        while dag.is_active() or submission_queue or running_futures:
            for task_id in dag.get_ready():
                reason = should_skip(config, state, task_id)
                if reason:
                    state.set_skipped(task_id, reason)
                    print(...)
                    dag.done(task_id)
                else:
                    submission_queue.append(task_id)

            made_progress = False
            remaining = deque()
            while submission_queue and len(running_futures) < config.settings.max_parallel:
                task_id = submission_queue.popleft()
                project = config.tasks[task_id].project
                if project in busy_projects:
                    remaining.append(task_id)
                    continue
                busy_projects.add(project)
                future = executor.submit(_execute_task, config, state, task_id, project_locks[project])
                running_futures[future] = task_id
                made_progress = True

            submission_queue.extendleft(reversed(remaining))

            if running_futures:
                done, _ = wait(running_futures, return_when=FIRST_COMPLETED)
                for future in done:
                    task_id = running_futures.pop(future)
                    result = _coerce_future_result(future, config, state, task_id)
                    busy_projects.remove(result.project)
                    dag.done(task_id)
                    made_progress = True

            if not made_progress and not running_futures and submission_queue:
                raise RuntimeError("Scheduler deadlock: queued tasks but nothing running")

    print(state.summary())
    notify_pipeline_done(config.settings, state)
    return state
```

## Deadlock Expectations

Under this design, deadlock should not occur unless there is a scheduler bug.

Why:

- DAG cycles are already rejected in config validation
- only one lock is taken per worker
- lock ownership is per-project and never nested
- `busy_projects` prevents executor starvation by same-project tasks

Add one defensive check:

- if the queue is non-empty, no futures are running, and no submissions were possible, raise `RuntimeError`

This should never trigger in correct code.

## Required Tests

Phase 2A should ship with automated tests. The tests can use a fake worker or monkeypatched `run_task`.

Minimum test list:

1. `max_parallel=1` behaves like serial execution
2. Two independent tasks from different projects run concurrently
3. Two ready tasks from the same project do not overlap
4. Upstream failure causes downstream skip in parallel mode
5. A skipped ready task still advances the DAG and releases its downstream
6. Worker exception becomes task failure and does not hang the scheduler
7. `run -p PROJECT` still includes upstream dependencies under parallel scheduling
8. Pipeline exit code is `1` if any task failed
9. Notification function is called once per failed task
10. `codex-review` with no diff becomes `skipped`, not `done`

Nice-to-have tests:

11. Queue fairness across projects when one project has many ready tasks
12. State JSON stays readable throughout a multi-task run

## Acceptance Criteria

Phase 2A is complete when all of the following are true:

- parallel runs respect `max_parallel`
- no same-project overlap occurs
- current failure semantics are unchanged
- downstream skip behavior is unchanged
- state files remain valid JSON throughout execution
- notifications are not duplicated
- `run -p ...` still works
- no dashboard or resume work is required to use the feature

## Phase 2B Handoff

Once Phase 2A is stable, Phase 2B can add:

- `retry`
- resume from latest run
- inherit only `done`
- recompute everything else under the current config

Phase 2B should reuse the Phase 2A scheduler loop instead of creating a separate resume-specific execution path.
