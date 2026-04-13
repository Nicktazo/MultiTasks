"""SSE endpoint tests — unit tests for helpers + integration tests for /api/events."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import time

import http.client

import pytest

from http.server import ThreadingHTTPServer

from core.dashboard import _DashboardHandler
from core.run_api import PipelineRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler(state_dir: str, runner=None):
    """Create a handler class with injected state_dir and runner."""
    template_dir = os.path.join(os.path.dirname(__file__), os.pardir, "templates")
    template_path = os.path.normpath(os.path.join(template_dir, "dashboard.html"))

    if runner is None:
        runner = PipelineRunner()

    return type(
        "_SSETestHandler",
        (_DashboardHandler,),
        {"state_dir": state_dir, "template_path": template_path,
         "runner": runner},
    )


def _make_state_file(state_dir: str, run_id: str) -> str:
    os.makedirs(state_dir, exist_ok=True)
    data = {"run_id": run_id, "created_at": "2026-04-13T10:00:00", "tasks": {}}
    path = os.path.join(state_dir, f"{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# Unit tests (no sockets, no HTTP)
# ---------------------------------------------------------------------------

class TestWriteSSE:
    """Test _write_sse output format."""

    def _make_instance(self):
        """Create a handler instance with a mock wfile."""
        handler_cls = _make_handler("/tmp/nonexistent")
        # Create a minimal instance without going through __init__
        instance = handler_cls.__new__(handler_cls)
        instance.wfile = io.BytesIO()
        return instance

    def test_write_sse_format(self):
        instance = self._make_instance()
        instance._write_sse("run-status", {"status": "idle"})
        output = instance.wfile.getvalue().decode("utf-8")
        assert output.startswith("event: run-status\n")
        assert "data: " in output
        assert output.endswith("\n\n")
        data_line = output.split("\n")[1]
        parsed = json.loads(data_line.removeprefix("data: "))
        assert parsed == {"status": "idle"}

    def test_write_sse_unicode(self):
        instance = self._make_instance()
        instance._write_sse("test", {"msg": "Hello \u4e16\u754c"})
        output = instance.wfile.getvalue().decode("utf-8")
        assert "\u4e16\u754c" in output
        data_line = output.split("\n")[1]
        parsed = json.loads(data_line.removeprefix("data: "))
        assert parsed["msg"] == "Hello \u4e16\u754c"


class TestGetLatestStateKey:
    """Test _get_latest_state_key helper."""

    def _make_instance(self, state_dir: str):
        handler_cls = _make_handler(state_dir)
        instance = handler_cls.__new__(handler_cls)
        instance.state_dir = state_dir
        return instance

    def test_get_latest_state_key_empty(self, tmp_path):
        state_dir = str(tmp_path / "state")
        instance = self._make_instance(state_dir)
        assert instance._get_latest_state_key() == ("", 0.0)

    def test_get_latest_state_key_detects_mtime_change(self, tmp_path):
        state_dir = str(tmp_path / "state")
        instance = self._make_instance(state_dir)

        _make_state_file(state_dir, "20260413-100000")
        key1 = instance._get_latest_state_key()
        assert key1[0] == "20260413-100000.json"
        assert key1[1] > 0

        # Wait briefly to ensure mtime changes
        time.sleep(0.05)
        path = os.path.join(state_dir, "20260413-100000.json")
        with open(path, "a") as f:
            f.write(" ")
        key2 = instance._get_latest_state_key()
        assert key2[0] == key1[0]
        assert key2[1] > key1[1]

    def test_get_latest_state_key_detects_new_file(self, tmp_path):
        """A new run file is detected even if mtime ordering is ambiguous."""
        state_dir = str(tmp_path / "state")
        instance = self._make_instance(state_dir)

        _make_state_file(state_dir, "20260413-100000")
        key1 = instance._get_latest_state_key()
        assert key1[0] == "20260413-100000.json"

        _make_state_file(state_dir, "20260413-110000")
        key2 = instance._get_latest_state_key()
        assert key2[0] == "20260413-110000.json"
        assert key2 != key1


# ---------------------------------------------------------------------------
# Integration tests (real HTTP, ThreadingHTTPServer)
# ---------------------------------------------------------------------------

@pytest.fixture
def sse_server():
    """Start a ThreadingHTTPServer with a PipelineRunner for SSE tests."""
    tmp = tempfile.mkdtemp(prefix="mt_sse_test_")
    state_dir = os.path.join(tmp, "state")

    runner = PipelineRunner()
    handler = _make_handler(state_dir, runner)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    yield port, state_dir, runner

    server.shutdown()
    server.server_close()
    shutil.rmtree(tmp, ignore_errors=True)


def test_sse_endpoint_headers(sse_server):
    """GET /api/events returns Content-Type: text/event-stream."""
    port, _, _ = sse_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        assert resp.status == 200
        assert "text/event-stream" in resp.getheader("Content-Type", "")
        assert resp.getheader("Cache-Control") == "no-cache"
    finally:
        conn.close()


def test_sse_sends_initial_run_status(sse_server):
    """First SSE event should be run-status with idle status."""
    port, _, _ = sse_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        assert resp.status == 200

        # Read enough bytes to capture the first event
        # SSE events end with \n\n
        buf = b""
        while b"\n\n" not in buf:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk

        text = buf.decode("utf-8")
        assert "event: run-status" in text
        # Extract the data line
        for line in text.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
                assert data["status"] == "idle"
                break
        else:
            pytest.fail("No data line found in SSE output")
    finally:
        conn.close()
