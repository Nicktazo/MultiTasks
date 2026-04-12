"""Task state machine + atomic persistence + log files."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class TaskState:
    task_id: str
    status: str = PENDING            # pending | running | done | failed | skipped
    started_at: str | None = None    # ISO 8601
    finished_at: str | None = None
    duration_s: float | None = None
    exit_code: int | None = None
    error: str = ""                  # error summary (truncated 500 chars)
    log_file: str | None = None      # full log path (relative to MultiTasks/)
    git_baseline: str | None = None  # git commit hash before task
    git_dirty: list[str] = field(default_factory=list)  # dirty files before task
    review_scope: str | None = None  # "possibly_contaminated" if review target was dirty
    depends_on: list[str] | None = None  # dependency snapshot from config at run time


class RunState:
    """Thread-safe run state with atomic persistence."""

    def __init__(self, state_dir: str = "state", run_id: str | None = None):
        self.state_dir = state_dir
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.created_at = datetime.now().isoformat()
        self.tasks: dict[str, TaskState] = {}
        self._lock = threading.Lock()

    @property
    def file_path(self) -> str:
        return os.path.join(self.state_dir, f"{self.run_id}.json")

    def init_tasks(self, task_ids: list[str], config=None) -> None:
        """Initialize all tasks as pending, optionally recording dependency snapshot."""
        with self._lock:
            for tid in task_ids:
                deps = None
                if config is not None:
                    task = config.tasks.get(tid)
                    if task:
                        deps = sorted(task.depends_on)
                self.tasks[tid] = TaskState(task_id=tid, depends_on=deps)
            self._save_unlocked()

    def set_running(self, task_id: str, git_baseline: str, git_dirty: list[str]) -> None:
        with self._lock:
            t = self.tasks[task_id]
            t.status = RUNNING
            t.started_at = datetime.now().isoformat()
            t.git_baseline = git_baseline
            t.git_dirty = git_dirty
            self._save_unlocked()

    def set_done(self, task_id: str, exit_code: int, log_file: str | None) -> None:
        with self._lock:
            t = self.tasks[task_id]
            t.status = DONE
            t.exit_code = exit_code
            t.log_file = log_file
            t.finished_at = datetime.now().isoformat()
            if t.started_at:
                start = datetime.fromisoformat(t.started_at)
                end = datetime.fromisoformat(t.finished_at)
                t.duration_s = round((end - start).total_seconds(), 1)
            self._save_unlocked()

    def set_failed(self, task_id: str, error: str, log_file: str | None) -> None:
        with self._lock:
            t = self.tasks[task_id]
            t.status = FAILED
            t.error = error[:500]
            t.log_file = log_file
            t.finished_at = datetime.now().isoformat()
            if t.started_at:
                start = datetime.fromisoformat(t.started_at)
                end = datetime.fromisoformat(t.finished_at)
                t.duration_s = round((end - start).total_seconds(), 1)
            self._save_unlocked()

    def set_skipped(self, task_id: str, reason: str) -> None:
        with self._lock:
            t = self.tasks[task_id]
            t.status = SKIPPED
            t.error = reason[:500]
            t.finished_at = datetime.now().isoformat()
            self._save_unlocked()

    def set_review_scope(self, task_id: str, review_scope: str) -> None:
        with self._lock:
            self.tasks[task_id].review_scope = review_scope
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        """Internal save. Caller must hold _lock. Atomic write."""
        os.makedirs(self.state_dir, exist_ok=True)
        data = {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "tasks": {tid: asdict(ts) for tid, ts in self.tasks.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.file_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save(self) -> None:
        """Public save (acquires lock)."""
        with self._lock:
            self._save_unlocked()

    @classmethod
    def load(cls, run_id: str, state_dir: str = "state") -> RunState:
        """Load a run state from JSON."""
        path = os.path.join(state_dir, f"{run_id}.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rs = cls(state_dir=state_dir, run_id=data["run_id"])
        rs.created_at = data["created_at"]
        for tid, td in data["tasks"].items():
            ts = TaskState(
                task_id=td["task_id"],
                status=td.get("status", PENDING),
                started_at=td.get("started_at"),
                finished_at=td.get("finished_at"),
                duration_s=td.get("duration_s"),
                exit_code=td.get("exit_code"),
                error=td.get("error", ""),
                log_file=td.get("log_file"),
                git_baseline=td.get("git_baseline"),
                git_dirty=td.get("git_dirty", []),
                review_scope=td.get("review_scope"),
                depends_on=td.get("depends_on"),
            )
            rs.tasks[tid] = ts
        return rs

    @classmethod
    def latest(cls, state_dir: str = "state") -> RunState | None:
        """Load the most recent run state."""
        if not os.path.isdir(state_dir):
            return None
        files = sorted(
            [f for f in os.listdir(state_dir) if f.endswith(".json")],
            reverse=True,
        )
        if not files:
            return None
        run_id = files[0].removesuffix(".json")
        return cls.load(run_id, state_dir)

    def done_task_ids(self) -> set[str]:
        """Return set of task IDs that are in done status."""
        return {tid for tid, ts in self.tasks.items() if ts.status == DONE}

    def summary(self) -> str:
        """Text summary of the run."""
        counts: dict[str, int] = {}
        for ts in self.tasks.values():
            counts[ts.status] = counts.get(ts.status, 0) + 1

        parts = []
        for status in [DONE, FAILED, SKIPPED, RUNNING, PENDING]:
            if counts.get(status, 0) > 0:
                parts.append(f"{status}={counts[status]}")

        lines = [
            f"Run: {self.run_id}",
            f"Tasks: {len(self.tasks)} ({', '.join(parts)})",
            "",
        ]

        for tid, ts in self.tasks.items():
            icon = {
                DONE: "[ok]", FAILED: "[FAIL]", SKIPPED: "[skip]",
                RUNNING: "[run]", PENDING: "[..]",
            }.get(ts.status, "[??]")
            dur = f" ({ts.duration_s}s)" if ts.duration_s is not None else ""
            err = f" - {ts.error}" if ts.error else ""
            lines.append(f"  {icon} {tid}{dur}{err}")

        return "\n".join(lines)


def build_resume_state(
    old_state: RunState,
    current_task_ids: list[str],
    state_dir: str = "state",
    config=None,
) -> tuple[RunState, set[str]]:
    """Build a new RunState for a retry run, inheriting only done tasks.

    Args:
        old_state: Previous run's state.
        current_task_ids: Task IDs from current config (in topo order).
        state_dir: Directory for the new state file.
        config: Current Config. If provided, a done task is only inherited
            when its dependency signature matches what was recorded in the
            old state. If the old state has no depends_on snapshot (pre-2B
            state file), deps comparison is skipped and the task is inherited.

    Returns:
        (new_state, inherited_done) where inherited_done is the set of
        task IDs that were carried over as done from the old run.

    Rules:
        - Old done + deps unchanged (or unknown) → inherit as done
        - Old done + deps changed → pending (re-run)
        - Old running/failed/skipped → pending (re-run)
        - Old task not in current config → ignored
        - New task not in old state → pending
    """
    new_state = RunState(state_dir=state_dir)
    inherited_done: set[str] = set()

    old_done = old_state.done_task_ids()

    with new_state._lock:
        for tid in current_task_ids:
            can_inherit = tid in old_done

            # Check dependency signature if both sides available
            if can_inherit and config is not None:
                old_ts = old_state.tasks[tid]
                current_task = config.tasks.get(tid)
                if old_ts.depends_on is not None and current_task is not None:
                    current_deps = sorted(current_task.depends_on)
                    if old_ts.depends_on != current_deps:
                        can_inherit = False

            if can_inherit:
                old_ts = old_state.tasks[tid]
                new_state.tasks[tid] = TaskState(
                    task_id=tid,
                    status=DONE,
                    started_at=old_ts.started_at,
                    finished_at=old_ts.finished_at,
                    duration_s=old_ts.duration_s,
                    exit_code=old_ts.exit_code,
                    error=old_ts.error,
                    log_file=old_ts.log_file,
                    git_baseline=old_ts.git_baseline,
                    git_dirty=list(old_ts.git_dirty),
                    review_scope=old_ts.review_scope,
                )
                inherited_done.add(tid)
            else:
                new_state.tasks[tid] = TaskState(task_id=tid)
        new_state._save_unlocked()

    return new_state, inherited_done
