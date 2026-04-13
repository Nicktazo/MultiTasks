"""Tests for core/events.py — EventLogger."""

from __future__ import annotations

import json
import os
import threading

import pytest

from core.events import EventLogger


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_log_writes_one_json_line(tmp_path):
    """A single log() call writes exactly one JSON line."""
    logger = EventLogger(str(tmp_path), "run-1")
    logger.log("task_running", task_id="projA:t1", status="running", message="Started")

    with open(logger.path, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["type"] == "task_running"
    assert record["task_id"] == "projA:t1"
    assert record["status"] == "running"
    assert record["message"] == "Started"
    assert record["run_id"] == "run-1"
    assert "ts" in record


def test_multiple_log_calls_append(tmp_path):
    """Multiple log() calls append separate lines."""
    logger = EventLogger(str(tmp_path), "run-2")
    logger.log("run_started", message="Go")
    logger.log("task_running", task_id="a:t1")
    logger.log("task_done", task_id="a:t1")
    logger.log("run_finished", message="Done")

    with open(logger.path, "r") as f:
        lines = f.readlines()
    assert len(lines) == 4
    assert json.loads(lines[0])["type"] == "run_started"
    assert json.loads(lines[3])["type"] == "run_finished"


def test_unicode_payload_roundtrips(tmp_path):
    """Non-ASCII characters survive write + read."""
    logger = EventLogger(str(tmp_path), "run-3")
    logger.log("task_done", task_id="proj:t1", message="Completed \u4e16\u754c")

    events = logger.read_all()
    assert len(events) == 1
    assert events[0]["message"] == "Completed \u4e16\u754c"


def test_read_all_returns_parsed_list(tmp_path):
    """read_all() returns a list of dicts."""
    logger = EventLogger(str(tmp_path), "run-4")
    logger.log("run_started")
    logger.log("task_running", task_id="a:t1", status="running")
    logger.log("run_finished", data={"counts": {"done": 1}})

    events = logger.read_all()
    assert len(events) == 3
    assert events[0]["type"] == "run_started"
    assert events[2]["data"] == {"counts": {"done": 1}}


def test_read_all_missing_file(tmp_path):
    """read_all() returns [] when the events file doesn't exist."""
    logger = EventLogger(str(tmp_path), "nonexistent-run")
    assert logger.read_all() == []


def test_concurrent_log_calls(tmp_path):
    """Multiple threads logging concurrently produce valid JSONL."""
    logger = EventLogger(str(tmp_path), "run-concurrent")
    barrier = threading.Barrier(10)

    def writer(i):
        barrier.wait()
        logger.log("task_done", task_id=f"proj:t{i}", message=f"Thread {i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = logger.read_all()
    assert len(events) == 10
    types = {e["type"] for e in events}
    assert types == {"task_done"}


def test_event_file_path(tmp_path):
    """Event file path matches <run_id>.events.jsonl."""
    logger = EventLogger(str(tmp_path), "20260413-120000")
    expected = os.path.join(str(tmp_path), "20260413-120000.events.jsonl")
    assert logger.path == expected


def test_logging_failure_does_not_raise(tmp_path):
    """If the state_dir is unwritable, log() prints to stderr but doesn't raise."""
    # Use a path that can't be created (file where dir expected)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    logger = EventLogger(str(blocker / "nested"), "run-fail")

    # Should not raise
    logger.log("test_event", message="should not crash")
