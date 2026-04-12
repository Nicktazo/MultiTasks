"""Reusable pipeline execution + background runner for dashboard."""

from __future__ import annotations

import threading
from dataclasses import dataclass, asdict
from datetime import datetime

from .config import load_config, ConfigError
from .dag import get_execution_order
from .scheduler import run_pipeline, resolve_project_scope
from .state import RunState, build_resume_state


# ---------------------------------------------------------------------------
# RunStatus — snapshot of background-runner state
# ---------------------------------------------------------------------------

@dataclass
class RunStatus:
    status: str = "idle"           # idle | running | done | failed
    mode: str = ""                 # run | retry
    project: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    run_id: str | None = None      # set only after pipeline completes
    error: str | None = None


# ---------------------------------------------------------------------------
# Reusable execution helpers (used by CLI + dashboard)
# ---------------------------------------------------------------------------

def execute_run(config_path: str, project: str | None = None) -> RunState:
    """Load config -> optional scope -> run_pipeline. Raises on error."""
    config = load_config(config_path)
    scope = resolve_project_scope(config, project) if project else None
    return run_pipeline(config, scope=scope)


def execute_retry(config_path: str) -> RunState:
    """Load config -> latest state -> build_resume_state -> run_pipeline. Raises on error."""
    config = load_config(config_path)
    old_state = RunState.latest(config.settings.state_dir)
    if old_state is None:
        raise RuntimeError("No previous run found")
    all_order = get_execution_order(config)
    new_state, inherited_done = build_resume_state(
        old_state, all_order, config.settings.state_dir, config=config,
    )
    return run_pipeline(config, state=new_state, inherited_done=inherited_done)


# ---------------------------------------------------------------------------
# PipelineRunner — thread-safe background execution for the dashboard
# ---------------------------------------------------------------------------

class PipelineRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = RunStatus()
        self._thread: threading.Thread | None = None

    def start(self, config_path: str, mode: str, project: str | None = None) -> dict:
        """Attempt to start a run. Returns response dict."""
        if mode not in ("run", "retry"):
            return {"ok": False, "errors": [f"Invalid mode: {mode!r}. Must be 'run' or 'retry'."]}

        with self._lock:
            if self._status.status == "running":
                return {"ok": False, "errors": ["Pipeline already running"]}

            now = datetime.now().isoformat()
            self._status = RunStatus(
                status="running",
                mode=mode,
                project=project,
                started_at=now,
            )
            snapshot = asdict(self._status)

            self._thread = threading.Thread(
                target=self._run_background,
                args=(config_path, mode, project),
                daemon=True,
            )
            self._thread.start()

        return {"ok": True, **snapshot}

    def get_status(self) -> dict:
        """Return current status as dict (thread-safe snapshot)."""
        with self._lock:
            return asdict(self._status)

    def _run_background(self, config_path: str, mode: str, project: str | None) -> None:
        """Background thread: call execute_run/execute_retry, update status on completion."""
        try:
            if mode == "run":
                state = execute_run(config_path, project=project)
            else:
                state = execute_retry(config_path)

            with self._lock:
                self._status.status = "done"
                self._status.run_id = state.run_id
                self._status.finished_at = datetime.now().isoformat()
        except Exception as e:
            with self._lock:
                self._status.status = "failed"
                self._status.error = str(e)[:500]
                self._status.finished_at = datetime.now().isoformat()
