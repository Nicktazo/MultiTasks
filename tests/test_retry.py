"""Phase 2B retry/resume tests — 9 required tests.

All tests monkeypatch run_task to avoid real CLI execution.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
import yaml

from core.config import load_config
from core.dag import get_execution_order
from core.runner import RunResult, GitSnapshot
from core.scheduler import run_pipeline
from core.state import (
    RunState, TaskState, DONE, FAILED, SKIPPED, RUNNING, PENDING,
    build_resume_state,
)


# ---------------------------------------------------------------------------
# Helpers (shared with test_scheduler.py pattern)
# ---------------------------------------------------------------------------

@dataclass
class FakeRunLog:
    calls: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, task_id: str):
        with self.lock:
            self.calls.append(task_id)


def _make_config_file(tmp: str, projects: dict, settings: dict | None = None) -> str:
    data = {"projects": projects}
    if settings:
        data["settings"] = settings
    path = os.path.join(tmp, "projects.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def _make_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@test",
                    "commit", "--allow-empty", "-m", "init", "-q"],
                   cwd=path, check=True, capture_output=True)


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp(prefix="mt_retry_")
    for name in ("projA", "projB"):
        _make_git_repo(os.path.join(tmp, name))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _default_settings(tmp):
    return {
        "max_parallel": 2, "notify": "none",
        "state_dir": os.path.join(tmp, "state"),
        "log_dir": os.path.join(tmp, "logs"),
    }


def _fake_run_task_factory(results=None, log_tracker=None):
    results = results or {}

    def fake(tool, prompt, cwd, timeout, log_dir, run_id, task_id,
             snapshot, baseline_commit=None, review_file=None):
        if log_tracker:
            log_tracker.record(task_id)
        if task_id in results:
            return results[task_id]
        log_path = os.path.join(log_dir, run_id, task_id.replace(":", "__") + ".log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            f.write(f"fake log for {task_id}\n")
        return RunResult(exit_code=0, success=True, log_file=log_path)

    return fake


def _fake_snapshot(cwd):
    return GitSnapshot(commit="fake123", dirty_files=[], is_git=True)


def _build_old_state(state_dir, task_states: dict[str, str],
                     depends_on: dict[str, list[str]] | None = None) -> RunState:
    """Create a fake old RunState with given task_id -> status mapping.

    Args:
        depends_on: Optional dict mapping task_id -> sorted dep list.
            Simulates a state file that recorded dependency snapshots.
    """
    depends_on = depends_on or {}
    old = RunState(state_dir=state_dir)
    with old._lock:
        for tid, status in task_states.items():
            ts = TaskState(task_id=tid, status=status)
            if status == DONE:
                ts.started_at = "2026-04-12T10:00:00"
                ts.finished_at = "2026-04-12T10:05:00"
                ts.duration_s = 300.0
                ts.exit_code = 0
                ts.git_baseline = "old123"
            elif status == FAILED:
                ts.error = "old failure"
            if tid in depends_on:
                ts.depends_on = depends_on[tid]
            old.tasks[tid] = ts
        old._save_unlocked()
    return old


# ---------------------------------------------------------------------------
# Test 1: retry only inherits done
# ---------------------------------------------------------------------------

def test_retry_inherits_only_done(tmp_workspace):
    state_dir = os.path.join(tmp_workspace, "state")
    old = _build_old_state(state_dir, {
        "projA:t1": DONE,
        "projA:t2": FAILED,
        "projA:t3": SKIPPED,
    })

    current_ids = ["projA:t1", "projA:t2", "projA:t3"]
    new_state, inherited = build_resume_state(old, current_ids, state_dir)

    assert inherited == {"projA:t1"}
    assert new_state.tasks["projA:t1"].status == DONE
    assert new_state.tasks["projA:t2"].status == PENDING
    assert new_state.tasks["projA:t3"].status == PENDING


# ---------------------------------------------------------------------------
# Test 2: old failed is not inherited, will be re-run
# ---------------------------------------------------------------------------

def test_old_failed_reruns(tmp_workspace):
    state_dir = os.path.join(tmp_workspace, "state")
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task", "tool": "claude"},
                {"id": "t2", "prompt": "task", "tool": "claude", "depends_on": ["t1"]},
            ],
        },
    }, _default_settings(tmp_workspace))

    config = load_config(cfg_path)

    # Old run: t1 done, t2 failed
    old = _build_old_state(state_dir, {"projA:t1": DONE, "projA:t2": FAILED})
    all_order = get_execution_order(config)
    new_state, inherited = build_resume_state(old, all_order, state_dir)

    log_tracker = FakeRunLog()

    with patch("core.scheduler.run_task", _fake_run_task_factory(log_tracker=log_tracker)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot):
        state = run_pipeline(config, state=new_state, inherited_done=inherited)

    # t1 inherited (not re-run), t2 re-run and now done
    assert "projA:t1" not in log_tracker.calls
    assert "projA:t2" in log_tracker.calls
    assert state.tasks["projA:t2"].status == DONE


# ---------------------------------------------------------------------------
# Test 3: old skipped is not inherited, will be recalculated
# ---------------------------------------------------------------------------

def test_old_skipped_recalculated(tmp_workspace):
    state_dir = os.path.join(tmp_workspace, "state")
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task", "tool": "claude"},
                {"id": "t2", "prompt": "task", "tool": "claude", "depends_on": ["t1"]},
            ],
        },
    }, _default_settings(tmp_workspace))

    config = load_config(cfg_path)

    # Old run: t1 failed, t2 skipped. Now both re-run.
    old = _build_old_state(state_dir, {"projA:t1": FAILED, "projA:t2": SKIPPED})
    all_order = get_execution_order(config)
    new_state, inherited = build_resume_state(old, all_order, state_dir)

    assert inherited == set()  # nothing inherited

    log_tracker = FakeRunLog()
    with patch("core.scheduler.run_task", _fake_run_task_factory(log_tracker=log_tracker)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot):
        state = run_pipeline(config, state=new_state, inherited_done=inherited)

    assert "projA:t1" in log_tracker.calls
    assert "projA:t2" in log_tracker.calls
    assert state.tasks["projA:t1"].status == DONE
    assert state.tasks["projA:t2"].status == DONE


# ---------------------------------------------------------------------------
# Test 4: old running is not inherited, will be re-run
# ---------------------------------------------------------------------------

def test_old_running_reruns(tmp_workspace):
    state_dir = os.path.join(tmp_workspace, "state")

    old = _build_old_state(state_dir, {"projA:t1": RUNNING})
    current_ids = ["projA:t1"]
    new_state, inherited = build_resume_state(old, current_ids, state_dir)

    assert inherited == set()
    assert new_state.tasks["projA:t1"].status == PENDING


# ---------------------------------------------------------------------------
# Test 5: config removes old task — no error
# ---------------------------------------------------------------------------

def test_deleted_task_ignored(tmp_workspace):
    state_dir = os.path.join(tmp_workspace, "state")

    # Old state has t1 and t2
    old = _build_old_state(state_dir, {"projA:t1": DONE, "projA:t2": DONE})

    # New config only has t1
    current_ids = ["projA:t1"]
    new_state, inherited = build_resume_state(old, current_ids, state_dir)

    assert "projA:t1" in new_state.tasks
    assert "projA:t2" not in new_state.tasks
    assert inherited == {"projA:t1"}


# ---------------------------------------------------------------------------
# Test 6: config adds new task — gets added as pending
# ---------------------------------------------------------------------------

def test_new_task_added(tmp_workspace):
    state_dir = os.path.join(tmp_workspace, "state")

    # Old state has t1 done
    old = _build_old_state(state_dir, {"projA:t1": DONE})

    # New config has t1 and t2 (new)
    current_ids = ["projA:t1", "projA:t2"]
    new_state, inherited = build_resume_state(old, current_ids, state_dir)

    assert new_state.tasks["projA:t1"].status == DONE
    assert new_state.tasks["projA:t2"].status == PENDING
    assert inherited == {"projA:t1"}


# ---------------------------------------------------------------------------
# Test 7: dependency change invalidates done inheritance
# ---------------------------------------------------------------------------

def test_dependency_change_invalidates_done(tmp_workspace):
    """When a task's deps changed since the old run, it must NOT be inherited."""
    state_dir = os.path.join(tmp_workspace, "state")
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task", "tool": "claude"},
                # t2 NOW depends on t1 (old run had no deps)
                {"id": "t2", "prompt": "task", "tool": "claude", "depends_on": ["t1"]},
            ],
        },
    }, _default_settings(tmp_workspace))

    config = load_config(cfg_path)

    # Old run: t2 was done with depends_on=[] (no deps at the time)
    old = _build_old_state(state_dir, {
        "projA:t1": DONE,
        "projA:t2": DONE,
    }, depends_on={
        "projA:t1": [],
        "projA:t2": [],       # old state recorded no deps for t2
    })

    all_order = get_execution_order(config)
    new_state, inherited = build_resume_state(old, all_order, state_dir, config=config)

    # t1 deps unchanged (still []) → inherited
    # t2 deps changed ([] → ["projA:t1"]) → NOT inherited, must re-run
    assert inherited == {"projA:t1"}
    assert new_state.tasks["projA:t1"].status == DONE
    assert new_state.tasks["projA:t2"].status == PENDING

    # Run pipeline: t1 inherited, t2 re-run
    log_tracker = FakeRunLog()
    with patch("core.scheduler.run_task", _fake_run_task_factory(log_tracker=log_tracker)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot):
        state = run_pipeline(config, state=new_state, inherited_done=inherited)

    assert "projA:t1" not in log_tracker.calls
    assert "projA:t2" in log_tracker.calls
    assert state.tasks["projA:t2"].status == DONE


def test_dependency_unchanged_inherits_done(tmp_workspace):
    """When deps match the old snapshot, done task is inherited normally."""
    state_dir = os.path.join(tmp_workspace, "state")
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task", "tool": "claude"},
                {"id": "t2", "prompt": "task", "tool": "claude", "depends_on": ["t1"]},
            ],
        },
    }, _default_settings(tmp_workspace))

    config = load_config(cfg_path)

    # Old run: both done, deps match current config
    old = _build_old_state(state_dir, {
        "projA:t1": DONE,
        "projA:t2": DONE,
    }, depends_on={
        "projA:t1": [],
        "projA:t2": ["projA:t1"],  # matches current config
    })

    all_order = get_execution_order(config)
    new_state, inherited = build_resume_state(old, all_order, state_dir, config=config)

    # Both inherited — deps unchanged
    assert inherited == {"projA:t1", "projA:t2"}
    assert new_state.tasks["projA:t1"].status == DONE
    assert new_state.tasks["projA:t2"].status == DONE


def test_pre2b_state_skips_dep_check(tmp_workspace):
    """Old state without depends_on snapshots (pre-2B) still inherits done tasks."""
    state_dir = os.path.join(tmp_workspace, "state")
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task", "tool": "claude"},
                {"id": "t2", "prompt": "task", "tool": "claude", "depends_on": ["t1"]},
            ],
        },
    }, _default_settings(tmp_workspace))

    config = load_config(cfg_path)

    # Old state has NO depends_on (pre-2B state file)
    old = _build_old_state(state_dir, {
        "projA:t1": DONE,
        "projA:t2": DONE,
    })
    # No depends_on passed → defaults to None

    all_order = get_execution_order(config)
    new_state, inherited = build_resume_state(old, all_order, state_dir, config=config)

    # Both inherited — dep comparison skipped when old snapshot is None
    assert inherited == {"projA:t1", "projA:t2"}


# ---------------------------------------------------------------------------
# Test 8: inherited done tasks are not executed but release downstream
# ---------------------------------------------------------------------------

def test_inherited_done_releases_downstream(tmp_workspace):
    state_dir = os.path.join(tmp_workspace, "state")
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task", "tool": "claude"},
                {"id": "t2", "prompt": "task", "tool": "claude", "depends_on": ["t1"]},
                {"id": "t3", "prompt": "task", "tool": "claude", "depends_on": ["t2"]},
            ],
        },
    }, _default_settings(tmp_workspace))

    config = load_config(cfg_path)

    # Old run: t1 done, t2 failed, t3 skipped
    old = _build_old_state(state_dir, {
        "projA:t1": DONE, "projA:t2": FAILED, "projA:t3": SKIPPED,
    })
    all_order = get_execution_order(config)
    new_state, inherited = build_resume_state(old, all_order, state_dir)

    assert inherited == {"projA:t1"}

    log_tracker = FakeRunLog()
    with patch("core.scheduler.run_task", _fake_run_task_factory(log_tracker=log_tracker)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot):
        state = run_pipeline(config, state=new_state, inherited_done=inherited)

    # t1 not executed (inherited), t2 and t3 re-run
    assert "projA:t1" not in log_tracker.calls
    assert "projA:t2" in log_tracker.calls
    assert "projA:t3" in log_tracker.calls
    assert state.tasks["projA:t1"].status == DONE
    assert state.tasks["projA:t2"].status == DONE
    assert state.tasks["projA:t3"].status == DONE


# ---------------------------------------------------------------------------
# Test 9: retry with no latest state gives clear error via CLI
# ---------------------------------------------------------------------------

def test_retry_no_latest_state_cli(tmp_workspace):
    """cmd_retry returns 1 and prints error when no previous run exists."""
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task", "tool": "claude"},
            ],
        },
    }, {
        **_default_settings(tmp_workspace),
        # Point to empty state dir — no previous run
        "state_dir": os.path.join(tmp_workspace, "empty_state"),
    })
    os.makedirs(os.path.join(tmp_workspace, "empty_state"), exist_ok=True)

    from multitasks import cmd_retry
    import argparse
    args = argparse.Namespace(config=cfg_path)
    rc = cmd_retry(args)

    assert rc == 1
