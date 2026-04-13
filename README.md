# MultiTasks

YAML-defined multi-project DAG orchestrator for `claude` / `codex` CLI tools.

Define tasks across multiple projects in a single `projects.yaml`, declare
dependencies between them, and MultiTasks will execute them in the right order
with bounded parallelism — one concurrent task per project, up to `max_parallel`
workers total.

## Quick Start

```bash
# 1. Clone / copy
git clone <repo-url> MultiTasks && cd MultiTasks

# 2. Install the one dependency
pip install pyyaml

# 3. Write a minimal projects.yaml
cat > projects.yaml << 'EOF'
projects:
  my-app:
    path: /absolute/path/to/my-app
    tasks:
      - id: fix-bug
        prompt: "Fix the login timeout bug in auth.py"
        tool: claude

      - id: review-fix
        prompt: "Review the fix-bug changes"
        tool: codex-review
        review_of: fix-bug
        depends_on: [fix-bug]

settings:
  max_parallel: 2
  dirty_workspace: warn
EOF

# 4. Validate, then run
python multitasks.py validate
python multitasks.py run
```

Tested with Python 3.12.

## Config Reference

`projects.yaml` schema:

```yaml
projects:
  <project-name>:                # unique name, used in task IDs
    path: /absolute/path         # must be an existing directory (and a git repo)
    tasks:
      - id: <local-id>           # unique within project
        prompt: "..."            # sent verbatim to the CLI tool
        tool: claude | codex | codex-review
        depends_on: []           # list of task IDs (local or "project:id")
        review_of: <task-id>     # codex-review only: which task to review

settings:
  max_parallel: 2                # int >= 1, concurrent worker threads
  timeout: 600                   # seconds per task
  notify: none                   # none | whatsapp | telegram
  dashboard_port: 18300          # int 1024-65535
  state_dir: state               # run state JSON files
  log_dir: logs                  # per-task log files
  dirty_workspace: warn          # warn | block | ignore
```

**Task ID resolution**: A bare ID like `fix-bug` is resolved to
`<owning-project>:fix-bug`. Cross-project references use the full form
`other-project:task-id`.

## CLI Commands

Global option: `-c / --config PATH` (default: `projects.yaml`).

### `validate [--check-workspace]`

Static config validation: YAML parse, schema checks, DAG cycle detection.
With `--check-workspace`, also checks each project path is a git repo and
reports dirty files.

### `run [-p PROJECT]`

Execute the pipeline. Without `-p`, runs all tasks. With `-p`, runs only
the named project's tasks plus all transitive upstream dependencies.

### `retry`

Re-run from the latest run. Inherits only **done** tasks from the previous
run — failed, skipped, running, and pending tasks are re-executed. If a done
task's dependency list has changed since the old run, it is invalidated and
re-executed too.

### `status`

Print a text summary of the most recent run (from `state_dir`).

### `dashboard [--port PORT] [--run-id RUN_ID]`

Start the read-only web dashboard. See the [Dashboard](#dashboard) section.

## Key Concepts

**DAG dependencies** — Tasks form a directed acyclic graph. A task only starts
after all its `depends_on` tasks are done. Cycles are rejected at validation.

**Cross-project references** — A task in project B can depend on a task in
project A using the full `projectA:task-id` syntax. This is how you chain work
across repos.

**`dirty_workspace`** — Controls what happens when a project has uncommitted
changes:
- `warn` (default): print a warning, proceed
- `block`: fail the task immediately
- `ignore`: no warning, proceed
- **Auto-escalation**: if task X is the `review_of` target for a
  `codex-review` task, its policy is automatically escalated to `block`
  regardless of the global setting (dirty workspace invalidates the review
  baseline).

**`codex-review` / `review_of`** — A review task diffs the workspace against
the git baseline recorded when its `review_of` target started. The diff is
injected into the review prompt. If there are no changes, the review is
skipped (not failed). `review_of` must also appear in `depends_on`.

**`run -p` scoping** — Computes the transitive closure of the project's tasks
and all upstream dependencies. Only those tasks are executed; the rest are
excluded entirely.

**`retry` semantics** — Loads the latest run state, then:
1. Done tasks with unchanged dependencies → inherited as done (not re-run)
2. Done tasks with changed dependencies → invalidated, re-run
3. Everything else (failed, skipped, running, pending) → re-run
4. New tasks in the current config → pending

**Project concurrency** — At most one task per project runs at any time (per-
project lock), even if `max_parallel` allows more workers. This prevents
concurrent git operations in the same repo.

## Dashboard

```bash
python multitasks.py dashboard              # latest run, port from config
python multitasks.py dashboard --port 9000  # custom port
python multitasks.py dashboard --run-id 20260412-100000  # specific run
```

The dashboard is served at `http://127.0.0.1:<port>` (default port: 18300).

Two tabs:

**Runs tab**: run metadata, per-task status/duration/errors, run selector
dropdown for historical runs, and an **event timeline** showing chronological
run events (task started/done/failed/skipped, run started/finished).
**Run All** and **Retry Latest** buttons launch pipelines directly from the
browser — status updates via SSE (Server-Sent Events) with automatic fallback
to 3-second polling if SSE disconnects. The `/api/events` endpoint pushes
`run-status` and `state-updated` events in real time (~1s latency).

**Config tab**: add/edit/delete projects and tasks, update settings, validate
the configuration — all from the browser. CLI YAML editing still works
alongside; the Config tab reads from disk on every load.

- **Run All / Retry Latest**: trigger a full pipeline run or retry from the
  latest state. Buttons are disabled while a run is in progress. A status bar
  shows idle/running/done/failed with timestamps.
- **Add Project**: combined form — project name, path, and first task submitted
  as a single operation (every project must have at least one task).
- **Add/Edit Task**: inline forms for prompt, tool, depends_on, review_of.
- **Delete**: blocked if other tasks reference the target (via depends_on or
  review_of). Cannot delete the last project.
- **Validate Config**: validates the saved-on-disk config and shows execution
  order or errors.
- **Saving from the dashboard rewrites `projects.yaml`, reformats it, and
  removes any YAML comments.** Hand-edited comments will be lost.

**Limitations**:
- No project-scoped runs from the dashboard (Run All runs everything)
- Localhost only — binds to 127.0.0.1, not accessible from other machines
- `--run-id` sets the initially selected run; the dropdown still lets you
  switch to others

## Architecture

```
multitasks.py          CLI entry point (argparse → subcommands)
core/
  config.py            YAML loading + full validation (Config/Project/Task/Settings)
  config_writer.py     Config serialization, atomic writes, mutation-with-validation
  dag.py               DAG construction (graphlib.TopologicalSorter), scoping, skip logic
  scheduler.py         Main orchestration loop: bounded ThreadPoolExecutor + per-project locks
  runner.py            CLI wrappers (claude/codex/codex-review), git snapshot, log writing
  state.py             RunState + TaskState: thread-safe state machine, atomic JSON persistence
  events.py            EventLogger: thread-safe append-only JSONL event log per run
  notify.py            WhatsApp/Telegram notifications via openclaw
  run_api.py           Reusable execute_run/execute_retry + PipelineRunner for dashboard
  dashboard.py         HTTP dashboard server (config editor + triggered runs)
templates/
  dashboard.html       Single-page dashboard frontend (runs + config + run controls)
tests/
  test_scheduler.py    Scheduler + config + DAG + runner integration tests
  test_retry.py        Retry / resume state tests
  test_dashboard.py    Dashboard HTTP endpoint tests (read-only + POST)
  test_sse.py          SSE endpoint unit + integration tests
  test_events.py       EventLogger unit tests
  test_config_writer.py Config writer round-trip, mutation, validation tests
  test_notify.py       Notification sanitization tests
  test_run_api.py      Run API: execute_run, execute_retry, PipelineRunner tests
```
