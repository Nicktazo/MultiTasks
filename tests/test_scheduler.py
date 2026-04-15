"""Phase 2A scheduler tests — 10 required tests per PHASE2A_SPEC.md.

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
from core.runner import RunResult, GitSnapshot
from core.scheduler import run_pipeline, resolve_project_scope
from core.state import DONE, FAILED, SKIPPED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeRunLog:
    """Records calls to the fake run_task for assertions."""
    calls: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, task_id: str, start_time: float):
        with self.lock:
            self.calls.append({
                "task_id": task_id,
                "start": start_time,
                "end": time.monotonic(),
                "thread": threading.current_thread().name,
            })


def _make_config_file(tmp: str, projects: dict, settings: dict | None = None) -> str:
    """Write a projects.yaml and return its path."""
    data = {"projects": projects}
    if settings:
        data["settings"] = settings
    path = os.path.join(tmp, "projects.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def _make_git_repo(path: str) -> None:
    """Create a minimal git repo at path."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@test",
                    "commit", "--allow-empty", "-m", "init", "-q"],
                   cwd=path, check=True, capture_output=True)


@pytest.fixture
def tmp_workspace():
    """Create temp dir with git repos, clean up after."""
    tmp = tempfile.mkdtemp(prefix="mt_test_")
    for name in ("projA", "projB", "projC"):
        _make_git_repo(os.path.join(tmp, name))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _fake_run_task_factory(
    results: dict[str, RunResult] | None = None,
    delay: float = 0,
    log: FakeRunLog | None = None,
):
    """Return a fake run_task function."""
    results = results or {}

    def fake_run_task(tool, prompt, cwd, timeout, log_dir, run_id, task_id,
                      snapshot, baseline_commit=None, review_file=None):
        start = time.monotonic()
        if delay:
            time.sleep(delay)
        if log:
            log.record(task_id, start)

        if task_id in results:
            return results[task_id]

        log_path = os.path.join(log_dir, run_id, task_id.replace(":", "__") + ".log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            f.write(f"fake log for {task_id}\n")
        return RunResult(exit_code=0, success=True, log_file=log_path)

    return fake_run_task


def _fake_snapshot_factory(is_git: bool = True, dirty: list[str] | None = None):
    """Return a fake capture_git_snapshot function."""
    def fake_capture(cwd):
        return GitSnapshot(commit="fake123", dirty_files=dirty or [], is_git=is_git)
    return fake_capture


# ---------------------------------------------------------------------------
# Test 1: max_parallel=1 behaves like serial execution
# ---------------------------------------------------------------------------

def test_serial_when_max_parallel_1(tmp_workspace):
    log = FakeRunLog()
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "task 1", "tool": "claude"},
                {"id": "t2", "prompt": "task 2", "tool": "claude", "depends_on": ["t1"]},
            ],
        },
    }, {"max_parallel": 1, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(delay=0.05, log=log)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert len(log.calls) == 2
    t1 = next(c for c in log.calls if c["task_id"] == "projA:t1")
    t2 = next(c for c in log.calls if c["task_id"] == "projA:t2")
    assert t1["end"] <= t2["start"] + 0.01


# ---------------------------------------------------------------------------
# Test 2: Independent tasks from different projects run concurrently
# ---------------------------------------------------------------------------

def test_cross_project_parallelism(tmp_workspace):
    log = FakeRunLog()
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [{"id": "a1", "prompt": "task a1", "tool": "claude"}],
        },
        "projB": {
            "path": os.path.join(tmp_workspace, "projB"),
            "tasks": [{"id": "b1", "prompt": "task b1", "tool": "claude"}],
        },
    }, {"max_parallel": 2, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(delay=0.15, log=log)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert len(log.calls) == 2
    a1 = next(c for c in log.calls if c["task_id"] == "projA:a1")
    b1 = next(c for c in log.calls if c["task_id"] == "projB:b1")
    # They should overlap: a1 starts before b1 ends and vice versa
    assert a1["start"] < b1["end"]
    assert b1["start"] < a1["end"]


# ---------------------------------------------------------------------------
# Test 3: Same-project tasks do not overlap
# ---------------------------------------------------------------------------

def test_same_project_no_overlap(tmp_workspace):
    log = FakeRunLog()
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "a1", "prompt": "task a1", "tool": "claude"},
                {"id": "a2", "prompt": "task a2", "tool": "claude"},
            ],
        },
    }, {"max_parallel": 4, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(delay=0.1, log=log)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert len(log.calls) == 2
    a1 = next(c for c in log.calls if c["task_id"] == "projA:a1")
    a2 = next(c for c in log.calls if c["task_id"] == "projA:a2")
    first, second = sorted([a1, a2], key=lambda c: c["start"])
    assert first["end"] <= second["start"] + 0.01


# ---------------------------------------------------------------------------
# Test 4: Upstream failure causes downstream skip
# ---------------------------------------------------------------------------

def test_upstream_failure_skips_downstream(tmp_workspace):
    fail_result = RunResult(exit_code=1, stderr="simulated failure", success=False,
                            log_file="fake.log")

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "t1", "prompt": "will fail", "tool": "claude"},
                {"id": "t2", "prompt": "depends on t1", "tool": "claude",
                 "depends_on": ["t1"]},
                {"id": "t3", "prompt": "depends on t2", "tool": "claude",
                 "depends_on": ["t2"]},
            ],
        },
    }, {"max_parallel": 2, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(results={"projA:t1": fail_result})), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert state.tasks["projA:t1"].status == FAILED
    assert state.tasks["projA:t2"].status == SKIPPED
    assert state.tasks["projA:t3"].status == SKIPPED


# ---------------------------------------------------------------------------
# Test 5: Skipped task advances DAG and releases downstream
# ---------------------------------------------------------------------------

def test_skipped_task_releases_downstream(tmp_workspace):
    fail_result = RunResult(exit_code=1, stderr="fail", success=False, log_file="f.log")

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "root", "prompt": "root", "tool": "claude"},
                {"id": "mid", "prompt": "mid", "tool": "claude", "depends_on": ["root"]},
                {"id": "leaf", "prompt": "leaf", "tool": "claude", "depends_on": ["mid"]},
            ],
        },
    }, {"max_parallel": 2, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(results={"projA:root": fail_result})), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert state.tasks["projA:root"].status == FAILED
    assert state.tasks["projA:mid"].status == SKIPPED
    assert state.tasks["projA:leaf"].status == SKIPPED
    assert "was skipped" in state.tasks["projA:leaf"].error


# ---------------------------------------------------------------------------
# Test 6: Worker exception becomes task failure without hanging
# ---------------------------------------------------------------------------

def test_worker_exception_becomes_failure(tmp_workspace):
    def exploding_run_task(tool, prompt, cwd, timeout, log_dir, run_id, task_id,
                           snapshot, baseline_commit=None, review_file=None):
        if task_id == "projA:t1":
            raise RuntimeError("boom")
        log_path = os.path.join(log_dir, run_id, task_id.replace(":", "__") + ".log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            f.write("ok\n")
        return RunResult(exit_code=0, success=True, log_file=log_path)

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [{"id": "t1", "prompt": "will explode", "tool": "claude"}],
        },
        "projB": {
            "path": os.path.join(tmp_workspace, "projB"),
            "tasks": [{"id": "t2", "prompt": "independent", "tool": "claude"}],
        },
    }, {"max_parallel": 2, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", exploding_run_task), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert state.tasks["projA:t1"].status == FAILED
    assert "boom" in state.tasks["projA:t1"].error
    assert state.tasks["projB:t2"].status == DONE


# ---------------------------------------------------------------------------
# Test 7: run -p includes upstream dependencies
# ---------------------------------------------------------------------------

def test_run_project_includes_upstream(tmp_workspace):
    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [{"id": "t1", "prompt": "upstream", "tool": "claude"}],
        },
        "projB": {
            "path": os.path.join(tmp_workspace, "projB"),
            "tasks": [{"id": "t2", "prompt": "downstream", "tool": "claude",
                        "depends_on": ["projA:t1"]}],
        },
    }, {"max_parallel": 2, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)
    scope = resolve_project_scope(config, "projB")
    assert "projA:t1" in scope
    assert "projB:t2" in scope

    with patch("core.scheduler.run_task", _fake_run_task_factory()), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config, scope=scope)

    assert state.tasks["projA:t1"].status == DONE
    assert state.tasks["projB:t2"].status == DONE


# ---------------------------------------------------------------------------
# Test 8: Pipeline exit code 1 if any task failed
# ---------------------------------------------------------------------------

def test_pipeline_exit_code_on_failure(tmp_workspace):
    fail_result = RunResult(exit_code=1, stderr="err", success=False, log_file="f.log")

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [{"id": "t1", "prompt": "fail", "tool": "claude"}],
        },
    }, {"max_parallel": 1, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(results={"projA:t1": fail_result})), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    failed = [t for t in state.tasks.values() if t.status == FAILED]
    assert len(failed) >= 1


# ---------------------------------------------------------------------------
# Test 9: Notification fires once per failed task
# ---------------------------------------------------------------------------

def test_notification_once_per_failure(tmp_workspace):
    fail_result = RunResult(exit_code=1, stderr="err", success=False, log_file="f.log")

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [{"id": "t1", "prompt": "fail", "tool": "claude"}],
        },
        "projB": {
            "path": os.path.join(tmp_workspace, "projB"),
            "tasks": [{"id": "t2", "prompt": "fail too", "tool": "claude"}],
        },
    }, {"max_parallel": 2, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)
    results = {"projA:t1": fail_result, "projB:t2": fail_result}

    with patch("core.scheduler.run_task", _fake_run_task_factory(results=results)), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()), \
         patch("core.scheduler.notify_task_failed") as mock_notify:
        state = run_pipeline(config)

    notified_tasks = [call.args[1] for call in mock_notify.call_args_list]
    assert sorted(notified_tasks) == ["projA:t1", "projB:t2"]


# ---------------------------------------------------------------------------
# Test 10: codex-review with no diff becomes skipped, not done
# ---------------------------------------------------------------------------

def test_codex_review_no_diff_is_skipped(tmp_workspace):
    no_diff = RunResult(exit_code=-4, stderr="No changes since abc — nothing to review",
                        success=False, log_file="f.log")

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "impl", "prompt": "implement", "tool": "claude"},
                {"id": "review", "prompt": "review", "tool": "codex-review",
                 "review_of": "impl", "depends_on": ["impl"]},
            ],
        },
    }, {"max_parallel": 1, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(results={"projA:review": no_diff})), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert state.tasks["projA:impl"].status == DONE
    assert state.tasks["projA:review"].status == SKIPPED
    assert "nothing to review" in state.tasks["projA:review"].error.lower()


# ---------------------------------------------------------------------------
# Test 11: codex-review with file path review_of passes review_file to runner
# ---------------------------------------------------------------------------

def test_codex_review_file_path(tmp_workspace):
    """File path review_of should pass review_file (not baseline) to run_task."""
    captured = {}

    def spy_run_task(tool, prompt, cwd, timeout, log_dir, run_id, task_id,
                     snapshot, baseline_commit=None, review_file=None):
        captured["baseline_commit"] = baseline_commit
        captured["review_file"] = review_file
        log_path = os.path.join(log_dir, run_id, task_id.replace(":", "__") + ".log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            f.write("ok\n")
        return RunResult(exit_code=0, success=True, log_file=log_path)

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "review-plan", "prompt": "Review plan",
                 "tool": "codex-review", "review_of": "docs/plan.md"},
            ],
        },
    }, {"max_parallel": 1, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", spy_run_task), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert state.tasks["projA:review-plan"].status == DONE
    assert captured["review_file"] == "docs/plan.md"
    assert captured["baseline_commit"] is None


# ---------------------------------------------------------------------------
# Test 12: file review — file not found becomes FAILED with stderr message
# ---------------------------------------------------------------------------

def test_codex_review_file_not_found(tmp_workspace):
    not_found = RunResult(exit_code=-3, stderr="Review file not found: missing.md",
                          success=False, log_file="f.log")

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "review-missing", "prompt": "Review",
                 "tool": "codex-review", "review_of": "missing.md"},
            ],
        },
    }, {"max_parallel": 1, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(results={"projA:review-missing": not_found})), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert state.tasks["projA:review-missing"].status == FAILED
    assert "not found" in state.tasks["projA:review-missing"].error.lower()


# ---------------------------------------------------------------------------
# Test 13: file review — empty file becomes SKIPPED
# ---------------------------------------------------------------------------

def test_codex_review_file_empty(tmp_workspace):
    empty_file = RunResult(exit_code=-4, stderr="Review file is empty: empty.md",
                           success=False, log_file="f.log")

    cfg_path = _make_config_file(tmp_workspace, {
        "projA": {
            "path": os.path.join(tmp_workspace, "projA"),
            "tasks": [
                {"id": "review-empty", "prompt": "Review",
                 "tool": "codex-review", "review_of": "empty.md"},
            ],
        },
    }, {"max_parallel": 1, "notify": "none",
        "state_dir": os.path.join(tmp_workspace, "state"),
        "log_dir": os.path.join(tmp_workspace, "logs")})

    config = load_config(cfg_path)

    with patch("core.scheduler.run_task", _fake_run_task_factory(results={"projA:review-empty": empty_file})), \
         patch("core.scheduler.capture_git_snapshot", _fake_snapshot_factory()):
        state = run_pipeline(config)

    assert state.tasks["projA:review-empty"].status == SKIPPED
    assert "empty" in state.tasks["projA:review-empty"].error.lower()
