"""Tests for core/runner.py — _run_codex_review_file."""

from __future__ import annotations

import os
import tempfile
import shutil

from core.runner import _run_codex_review_file


def test_review_file_not_found():
    """Missing file returns exit code -3."""
    tmp = tempfile.mkdtemp(prefix="mt_runner_")
    try:
        code, stdout, stderr = _run_codex_review_file(
            "Review this", tmp, 60, os.environ.copy(), "nonexistent.md"
        )
        assert code == -3
        assert "not found" in stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_review_file_empty():
    """Empty file returns exit code -4."""
    tmp = tempfile.mkdtemp(prefix="mt_runner_")
    try:
        path = os.path.join(tmp, "empty.md")
        with open(path, "w") as f:
            f.write("   \n")  # whitespace only
        code, stdout, stderr = _run_codex_review_file(
            "Review this", tmp, 60, os.environ.copy(), "empty.md"
        )
        assert code == -4
        assert "empty" in stderr.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_review_file_absolute_path():
    """Absolute path is used directly, not joined with cwd."""
    tmp = tempfile.mkdtemp(prefix="mt_runner_")
    try:
        path = os.path.join(tmp, "plan.md")
        with open(path, "w") as f:
            f.write("# My Plan\nSome content here.")
        # Use absolute path — cwd should be irrelevant
        code, stdout, stderr = _run_codex_review_file(
            "Review this", "/nonexistent-cwd", 60, os.environ.copy(), path
        )
        # Should NOT return -3 (file found), will return -2 (codex not found)
        # since we don't have codex installed in test env
        assert code != -3  # file was found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
