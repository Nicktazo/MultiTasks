"""Tests for core/run_api.py — execute_run, execute_retry, PipelineRunner."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock

import pytest
import yaml

from core.config import load_config
from core.run_api import execute_run, execute_retry, PipelineRunner
from core.state import RunState, DONE, FAILED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=path, check=True, capture_output=True,
    )


def _make_config_file(tmp: str, projects: dict, settings: dict | None = None) -> str:
    data = {"projects": projects}
    if settings:
        data["settings"] = settings
    path = os.path.join(tmp, "projects.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


@pytest.fixture
def tmp_workspace():
    tmp = tempfile.mkdtemp(prefix="mt_runapi_")
    _make_git_repo(os.path.join(tmp, "projA"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def _simple_config(tmp):
    """Return config_path for a minimal one-project, one-task config."""
    return _make_config_file(tmp, {
        "projA": {
            "path": os.path.join(tmp, "projA"),
            "tasks": [{"id": "t1", "prompt": "do stuff", "tool": "claude"}],
        },
    }, {"state_dir": os.path.join(tmp, "state"), "log_dir": os.path.join(tmp, "logs")})


# ---------------------------------------------------------------------------
# Unit: execute_run / execute_retry
# ---------------------------------------------------------------------------

def test_execute_run_calls_pipeline(tmp_workspace):
    """execute_run loads config and calls run_pipeline with correct args."""
    config_path = _simple_config(tmp_workspace)
    fake_state = MagicMock()
    fake_state.run_id = "test-run"
    fake_state.tasks = {}

    with patch("core.run_api.run_pipeline", return_value=fake_state) as mock_pipe, \
         patch("core.run_api.resolve_project_scope") as mock_scope:
        result = execute_run(config_path)

    mock_pipe.assert_called_once()
    mock_scope.assert_not_called()
    assert result is fake_state


def test_execute_run_with_project_scope(tmp_workspace):
    """execute_run passes scope when project is specified."""
    config_path = _simple_config(tmp_workspace)
    fake_state = MagicMock()
    fake_state.run_id = "test-run"
    fake_state.tasks = {}

    with patch("core.run_api.run_pipeline", return_value=fake_state) as mock_pipe, \
         patch("core.run_api.resolve_project_scope", return_value={"projA:t1"}) as mock_scope:
        result = execute_run(config_path, project="projA")

    mock_scope.assert_called_once()
    args, kwargs = mock_pipe.call_args
    assert kwargs.get("scope") == {"projA:t1"} or args[1] == {"projA:t1"}


def test_execute_retry_calls_pipeline(tmp_workspace):
    """execute_retry loads prior state and calls run_pipeline with resume args."""
    config_path = _simple_config(tmp_workspace)
    config = load_config(config_path)

    # Create a prior run state
    old_state = RunState(state_dir=config.settings.state_dir, run_id="old-run")
    old_state.init_tasks(["projA:t1"], config=config)
    old_state.set_running("projA:t1", "abc", [])
    old_state.set_done("projA:t1", 0, "log.txt")

    fake_new_state = MagicMock()
    fake_new_state.run_id = "new-run"
    fake_new_state.tasks = {}

    with patch("core.run_api.run_pipeline", return_value=fake_new_state) as mock_pipe:
        result = execute_retry(config_path)

    mock_pipe.assert_called_once()
    _, kwargs = mock_pipe.call_args
    assert "state" in kwargs
    assert "inherited_done" in kwargs
    assert result is fake_new_state


def test_execute_retry_no_previous_run(tmp_workspace):
    """execute_retry raises RuntimeError when no prior run exists."""
    config_path = _simple_config(tmp_workspace)

    with pytest.raises(RuntimeError, match="No previous run found"):
        execute_retry(config_path)


# ---------------------------------------------------------------------------
# Unit: PipelineRunner
# ---------------------------------------------------------------------------

def test_runner_get_status_idle():
    """Fresh PipelineRunner returns idle status."""
    runner = PipelineRunner()
    st = runner.get_status()
    assert st["status"] == "idle"
    assert st["mode"] == ""
    assert st["run_id"] is None


def test_runner_start_sets_running(tmp_workspace):
    """PipelineRunner.start() sets status to running and returns ok."""
    config_path = _simple_config(tmp_workspace)
    gate = threading.Event()

    def slow_pipeline(*a, **kw):
        gate.wait(timeout=5)
        state = MagicMock()
        state.run_id = "done-id"
        return state

    runner = PipelineRunner()
    with patch("core.run_api.run_pipeline", side_effect=slow_pipeline):
        result = runner.start(config_path, "run")

    assert result["ok"] is True
    assert result["status"] == "running"
    assert result["mode"] == "run"

    st = runner.get_status()
    assert st["status"] == "running"

    gate.set()  # release the background thread
    time.sleep(0.2)


def test_runner_busy_returns_conflict(tmp_workspace):
    """Starting a run while already running returns conflict."""
    config_path = _simple_config(tmp_workspace)
    gate = threading.Event()

    def slow_pipeline(*a, **kw):
        gate.wait(timeout=5)
        state = MagicMock()
        state.run_id = "x"
        return state

    runner = PipelineRunner()
    with patch("core.run_api.run_pipeline", side_effect=slow_pipeline):
        runner.start(config_path, "run")
        result = runner.start(config_path, "run")

    assert result["ok"] is False
    assert "already running" in result["errors"][0].lower()

    gate.set()
    time.sleep(0.2)


def test_runner_completes_to_done(tmp_workspace):
    """After pipeline completes, status becomes done with run_id."""
    config_path = _simple_config(tmp_workspace)
    gate = threading.Event()

    fake_state = MagicMock()
    fake_state.run_id = "completed-123"
    fake_state.tasks = {}

    def gated_pipeline(*a, **kw):
        gate.wait(timeout=5)
        return fake_state

    runner = PipelineRunner()
    with patch("core.run_api.run_pipeline", side_effect=gated_pipeline):
        runner.start(config_path, "run")

        # Still running
        assert runner.get_status()["status"] == "running"

        # Release
        gate.set()
        time.sleep(0.3)

        st = runner.get_status()
        assert st["status"] == "done"
        assert st["run_id"] == "completed-123"
        assert st["finished_at"] is not None


def test_runner_error_becomes_failed(tmp_workspace):
    """Pipeline exception sets status to failed with error string."""
    config_path = _simple_config(tmp_workspace)

    def failing_pipeline(*a, **kw):
        raise RuntimeError("disk full")

    runner = PipelineRunner()
    with patch("core.run_api.run_pipeline", side_effect=failing_pipeline):
        runner.start(config_path, "run")
        time.sleep(0.3)

        st = runner.get_status()
        assert st["status"] == "failed"
        assert "disk full" in st["error"]


def test_runner_invalid_mode():
    """start() with bad mode returns error without starting."""
    runner = PipelineRunner()
    result = runner.start("fake.yaml", "invalid-mode")
    assert result["ok"] is False
    assert "Invalid mode" in result["errors"][0]
    assert runner.get_status()["status"] == "idle"
