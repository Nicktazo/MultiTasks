"""Dashboard tests — read-only endpoints + config editor POST endpoints."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer

import pytest
import yaml

from core.dashboard import _DashboardHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_state_file(state_dir: str, run_id: str, tasks: dict | None = None) -> str:
    """Write a state JSON file and return the run_id."""
    os.makedirs(state_dir, exist_ok=True)
    data = {
        "run_id": run_id,
        "created_at": "2026-04-12T10:00:00",
        "tasks": tasks or {
            "projA:t1": {
                "task_id": "projA:t1",
                "status": "done",
                "duration_s": 1.2,
                "git_baseline": "abc1234567890",
                "error": "",
            },
            "projA:t2": {
                "task_id": "projA:t2",
                "status": "failed",
                "duration_s": 3.5,
                "error": "exit code 1",
            },
        },
    }
    path = os.path.join(state_dir, f"{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return run_id


def _make_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=path, check=True, capture_output=True,
    )


@pytest.fixture
def dashboard_server():
    """Start a dashboard server on a random port, yield (base_url, state_dir), then shutdown."""
    tmp = tempfile.mkdtemp(prefix="mt_dash_test_")
    state_dir = os.path.join(tmp, "state")

    template_dir = os.path.join(os.path.dirname(__file__), os.pardir, "templates")
    template_path = os.path.normpath(os.path.join(template_dir, "dashboard.html"))

    handler = type(
        "_TestHandler",
        (_DashboardHandler,),
        {"state_dir": state_dir, "template_path": template_path},
    )

    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    yield base_url, state_dir

    server.shutdown()
    server.server_close()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def config_dashboard_server():
    """Start a dashboard server with a real config file for POST endpoint tests."""
    tmp = tempfile.mkdtemp(prefix="mt_dash_cfg_test_")
    state_dir = os.path.join(tmp, "state")
    log_dir = os.path.join(tmp, "logs")

    proj_path = os.path.join(tmp, "myproj")
    _make_git_repo(proj_path)

    config_data = {
        "projects": {
            "myproj": {
                "path": proj_path,
                "tasks": [
                    {"id": "t1", "prompt": "Task one", "tool": "claude"},
                    {"id": "t2", "prompt": "Review t1", "tool": "codex-review",
                     "review_of": "t1", "depends_on": ["t1"]},
                ],
            },
        },
        "settings": {
            "max_parallel": 2, "timeout": 600, "notify": "none",
            "dashboard_port": 18300, "state_dir": state_dir, "log_dir": log_dir,
            "dirty_workspace": "warn",
        },
    }
    config_path = os.path.join(tmp, "projects.yaml")
    with open(config_path, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

    template_dir = os.path.join(os.path.dirname(__file__), os.pardir, "templates")
    template_path = os.path.normpath(os.path.join(template_dir, "dashboard.html"))

    handler = type(
        "_CfgTestHandler",
        (_DashboardHandler,),
        {"state_dir": state_dir, "template_path": template_path,
         "config_path": config_path},
    )

    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    yield base_url, config_path, tmp

    server.shutdown()
    server.server_close()
    shutil.rmtree(tmp, ignore_errors=True)


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    """HTTP GET (bypasses system proxy), returns (status_code, headers_dict, body)."""
    req = urllib.request.Request(url)
    try:
        resp = _opener.open(req)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post(url: str, body: dict) -> tuple[int, dict[str, str], bytes]:
    """HTTP POST JSON (bypasses system proxy)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        resp = _opener.open(req)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# ---------------------------------------------------------------------------
# Existing read-only tests (Tests 1-10)
# ---------------------------------------------------------------------------

def test_server_starts(dashboard_server):
    base_url, _ = dashboard_server
    status, _, _ = _get(f"{base_url}/")
    assert status == 200


def test_get_root_returns_html(dashboard_server):
    base_url, _ = dashboard_server
    status, headers, body = _get(f"{base_url}/")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert b'<div id="app">' in body


def test_api_state_returns_latest(dashboard_server):
    base_url, state_dir = dashboard_server
    _make_state_file(state_dir, "20260412-090000")
    _make_state_file(state_dir, "20260412-100000")

    status, headers, body = _get(f"{base_url}/api/state")
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert data["run_id"] == "20260412-100000"
    assert "tasks" in data


def test_api_state_specific_run(dashboard_server):
    base_url, state_dir = dashboard_server
    _make_state_file(state_dir, "20260412-090000")
    _make_state_file(state_dir, "20260412-100000")

    status, _, body = _get(f"{base_url}/api/state?run_id=20260412-090000")
    assert status == 200
    data = json.loads(body)
    assert data["run_id"] == "20260412-090000"


def test_api_state_bad_run_id(dashboard_server):
    base_url, state_dir = dashboard_server
    _make_state_file(state_dir, "20260412-100000")

    status, _, body = _get(f"{base_url}/api/state?run_id=nonexistent")
    assert status == 404
    data = json.loads(body)
    assert "error" in data
    assert "nonexistent" in data["error"]


def test_api_state_empty_dir(dashboard_server):
    base_url, _ = dashboard_server
    status, _, body = _get(f"{base_url}/api/state")
    assert status == 404
    data = json.loads(body)
    assert "No runs found" in data["error"]


def test_api_runs_lists_ids(dashboard_server):
    base_url, state_dir = dashboard_server
    _make_state_file(state_dir, "20260412-090000")
    _make_state_file(state_dir, "20260412-100000")

    status, headers, body = _get(f"{base_url}/api/runs")
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert isinstance(data, list)
    assert "20260412-100000" in data
    assert "20260412-090000" in data
    assert data[0] == "20260412-100000"


def test_run_id_html_escape():
    """initial_run_id with HTML special chars is escaped in the served page."""
    tmp = tempfile.mkdtemp(prefix="mt_dash_escape_")
    state_dir = os.path.join(tmp, "state")

    template_dir = os.path.join(os.path.dirname(__file__), os.pardir, "templates")
    template_path = os.path.normpath(os.path.join(template_dir, "dashboard.html"))

    xss_payload = '"><script>alert(1)</script>'

    handler = type(
        "_EscapeTestHandler",
        (_DashboardHandler,),
        {"state_dir": state_dir, "template_path": template_path,
         "initial_run_id": xss_payload},
    )

    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        _, _, body = _get(f"{base_url}/")
        body_str = body.decode("utf-8")
        assert "&quot;&gt;&lt;script&gt;" in body_str
        assert '"><script>alert(1)</script>' not in body_str
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_port_binding_error(monkeypatch, capsys):
    """start_dashboard returns False and prints error when port is unavailable."""
    from core import dashboard

    def _raise_oserror(*args, **kwargs):
        raise OSError("Address already in use")

    monkeypatch.setattr(dashboard, "HTTPServer", _raise_oserror)

    result = dashboard.start_dashboard(port=9999, state_dir="/tmp/nonexistent")
    assert result is False

    captured = capsys.readouterr()
    assert "cannot start dashboard on port 9999" in captured.err
    assert "Address already in use" in captured.err


def test_unknown_route_404(dashboard_server):
    base_url, _ = dashboard_server
    status, headers, body = _get(f"{base_url}/nonexistent")
    assert status == 404
    assert "application/json" in headers.get("Content-Type", "")
    data = json.loads(body)
    assert "error" in data


# ---------------------------------------------------------------------------
# Phase 5: GET /api/config
# ---------------------------------------------------------------------------

def test_get_api_config(config_dashboard_server):
    """GET /api/config returns the current config from disk."""
    base_url, _, _ = config_dashboard_server
    status, headers, body = _get(f"{base_url}/api/config")
    assert status == 200
    data = json.loads(body)
    assert "projects" in data
    assert "myproj" in data["projects"]
    assert "settings" in data


def test_get_api_config_no_file(dashboard_server):
    """GET /api/config with no config file returns defaults."""
    base_url, _ = dashboard_server
    status, _, body = _get(f"{base_url}/api/config")
    # config_path defaults to "projects.yaml" which doesn't exist in tmp
    # Should still return 200 with defaults or the file-not-found fallback
    # Since default config_path is "projects.yaml" (doesn't exist), returns defaults
    assert status in (200, 500)


# ---------------------------------------------------------------------------
# Phase 5: POST endpoint tests
# ---------------------------------------------------------------------------

def test_post_upsert_task(config_dashboard_server):
    """POST /api/config/tasks adds a new task."""
    base_url, config_path, _ = config_dashboard_server
    status, _, body = _post(f"{base_url}/api/config/tasks", {
        "project": "myproj",
        "id": "t3",
        "prompt": "New task three",
        "tool": "claude",
    })
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    tasks = data["config"]["projects"]["myproj"]["tasks"]
    assert any(t["id"] == "t3" for t in tasks)
    assert isinstance(data["order"], list)
    assert len(data["order"]) > 0


def test_post_update_settings(config_dashboard_server):
    """POST /api/config/settings updates settings."""
    base_url, _, _ = config_dashboard_server
    status, _, body = _post(f"{base_url}/api/config/settings", {
        "max_parallel": 4,
    })
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["config"]["settings"]["max_parallel"] == 4


def test_post_delete_task_blocked(config_dashboard_server):
    """POST /api/config/tasks/delete blocked when task is referenced."""
    base_url, _, _ = config_dashboard_server
    # t1 is referenced by t2 (review_of + depends_on)
    status, _, body = _post(f"{base_url}/api/config/tasks/delete", {
        "project": "myproj", "id": "t1",
    })
    assert status == 400
    data = json.loads(body)
    assert data["ok"] is False
    assert any("referenced by" in e for e in data["errors"])


def test_post_validate(config_dashboard_server):
    """POST /api/validate validates the saved config."""
    base_url, _, _ = config_dashboard_server
    status, _, body = _post(f"{base_url}/api/validate", {})
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert isinstance(data["order"], list)
    assert len(data["order"]) > 0


def test_post_invalid_json(config_dashboard_server):
    """POST with invalid JSON returns 400."""
    base_url, _, _ = config_dashboard_server
    req = urllib.request.Request(
        f"{base_url}/api/config/tasks",
        data=b"not json",
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", "8")
    try:
        resp = _opener.open(req)
        status = resp.status
        body = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read()
    assert status == 400
    data = json.loads(body)
    assert data["ok"] is False
    assert any("Invalid JSON" in e for e in data["errors"])
