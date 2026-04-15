"""Tests for core/config.py — _is_file_review detection."""

from core.config import _is_file_review


def test_file_paths_detected():
    """Various file path patterns should be detected as file review."""
    assert _is_file_review("plan.md") is True
    assert _is_file_review(".plan/foo.md") is True
    assert _is_file_review("docs/plan.md") is True
    assert _is_file_review("docs\\plan.md") is True
    assert _is_file_review("C:\\work\\plan.md") is True
    assert _is_file_review("/absolute/path.md") is True


def test_task_ids_detected():
    """Valid task IDs should NOT be detected as file review."""
    assert _is_file_review("t1") is False
    assert _is_file_review("fix-bug") is False
    assert _is_file_review("my_task_123") is False
    assert _is_file_review("projA:t1") is False
    assert _is_file_review("proj-A:task-1") is False
