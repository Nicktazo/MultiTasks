"""CLI wrapper for claude/codex/codex-review. Pure execution, no git operations."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class GitSnapshot:
    commit: str                      # HEAD commit hash (short)
    dirty_files: list[str] = field(default_factory=list)  # ALL git status --porcelain lines
    tracked_dirty: list[str] = field(default_factory=list)  # modified/staged (affect git diff)
    untracked: list[str] = field(default_factory=list)      # ?? lines (don't affect git diff)
    is_git: bool = False             # True if cwd is inside a git repo


@dataclass
class RunResult:
    exit_code: int                   # 0=success, -1=timeout, -2=command not found
    stdout: str = ""
    stderr: str = ""
    parsed: dict = field(default_factory=dict)  # JSON parse result if available
    success: bool = False
    log_file: str = ""               # log file path


def capture_git_snapshot(cwd: str) -> GitSnapshot:
    """Sole git state capture entry point. Called by scheduler only.

    Returns is_git=False if cwd is not inside a git repository.
    Callers must check is_git before trusting commit/dirty_files.
    """
    # First check if this is a git repo at all
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return GitSnapshot(commit="", is_git=False)
    except Exception:
        return GitSnapshot(commit="", is_git=False)

    commit = ""
    dirty: list[str] = []

    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            commit = r.stdout.strip()
    except Exception:
        pass

    tracked: list[str] = []
    untracked: list[str] = []
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().splitlines():
                if not line.strip():
                    continue
                dirty.append(line)
                if line.startswith("?? "):
                    untracked.append(line)
                else:
                    tracked.append(line)
    except Exception:
        pass

    return GitSnapshot(commit=commit, dirty_files=dirty,
                       tracked_dirty=tracked, untracked=untracked, is_git=True)


def _safe_filename(task_id: str) -> str:
    """Convert task_id to Windows-safe filename: vultr:rssvault-fix -> vultr__rssvault-fix"""
    return task_id.replace(":", "__")


def _write_log(log_path: str, task_id: str, tool: str, prompt: str,
               snapshot: GitSnapshot, stdout: str, stderr: str,
               exit_code: int, duration_s: float) -> None:
    """Write complete task log to file."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    dirty_str = "\n".join(f"  {f}" for f in snapshot.dirty_files) if snapshot.dirty_files else "(none)"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"=== TASK: {task_id} ===\n")
        f.write(f"Tool: {tool}\n")
        f.write(f"Started: {datetime.now().isoformat()}\n")
        f.write(f"Git baseline: {snapshot.commit or '(none)'}\n")
        f.write(f"Dirty files:\n{dirty_str}\n")
        f.write(f"Prompt: {prompt}\n")
        f.write("\n=== STDOUT ===\n")
        f.write(stdout or "(empty)")
        f.write("\n\n=== STDERR ===\n")
        f.write(stderr or "(empty)")
        f.write(f"\n\n=== RESULT ===\n")
        f.write(f"Exit code: {exit_code}\n")
        f.write(f"Duration: {duration_s}s\n")


def _try_parse_json(text: str) -> dict:
    """Try to parse JSON from output. Returns empty dict on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


def _run_claude(prompt: str, cwd: str, timeout: int, env: dict) -> tuple[int, str, str]:
    """Run claude CLI."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
    ]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout after {timeout}s"
    except FileNotFoundError:
        return -2, "", "claude command not found"


def _run_codex(prompt: str, cwd: str, timeout: int, env: dict) -> tuple[int, str, str]:
    """Run codex CLI."""
    cmd = ["codex", "exec", prompt, "--full-auto", "--json"]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout after {timeout}s"
    except FileNotFoundError:
        return -2, "", "codex command not found"


def _run_codex_review(prompt: str, cwd: str, timeout: int, env: dict,
                      baseline_commit: str | None) -> tuple[int, str, str]:
    """Run codex review, scoped to changes since baseline."""
    if not baseline_commit:
        return -3, "", "No baseline commit — review precondition not met"

    # Get diff since baseline
    try:
        r = subprocess.run(
            ["git", "diff", baseline_commit, "--"],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        diff = r.stdout.strip()
    except Exception as e:
        return -2, "", f"Failed to get diff: {e}"

    if not diff:
        # No changes is not a failure, but it's not a real review either.
        # Use exit code -4 so scheduler can distinguish from actual review.
        return -4, "", f"No changes since {baseline_commit} — nothing to review"

    # Construct review prompt with diff context
    constructed_prompt = (
        f"{prompt}\n\n"
        f"Review scope: changes since {baseline_commit}\n"
        f"Diff:\n{diff}"
    )

    cmd = ["codex", "exec", constructed_prompt, "--full-auto", "--json"]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout after {timeout}s"
    except FileNotFoundError:
        return -2, "", "codex command not found"


_MAX_REVIEW_FILE = 200_000  # 200KB cap


def _run_codex_review_file(prompt: str, cwd: str, timeout: int, env: dict,
                           review_file: str) -> tuple[int, str, str]:
    """Run codex review on a specific file's content."""
    path = review_file if os.path.isabs(review_file) else os.path.join(cwd, review_file)
    if not os.path.isfile(path):
        return -3, "", f"Review file not found: {review_file}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(_MAX_REVIEW_FILE)
    if not content.strip():
        return -4, "", f"Review file is empty: {review_file}"
    constructed_prompt = (
        f"{prompt}\n\n"
        f"Review target: {review_file}\n"
        f"Content:\n{content}"
    )
    cmd = ["codex", "exec", constructed_prompt, "--full-auto", "--json"]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout after {timeout}s"
    except FileNotFoundError:
        return -2, "", "codex command not found"


def run_task(tool: str, prompt: str, cwd: str, timeout: int,
             log_dir: str, run_id: str, task_id: str,
             snapshot: GitSnapshot, baseline_commit: str | None = None,
             review_file: str | None = None) -> RunResult:
    """Unified task execution entry point.

    Dispatches to tool-specific runner, writes log file, returns RunResult.
    Does NOT perform git operations — snapshot is for log header only.
    """
    env = {**os.environ, "NO_COLOR": "1"}
    start = datetime.now()

    if tool == "claude":
        exit_code, stdout, stderr = _run_claude(prompt, cwd, timeout, env)
    elif tool == "codex":
        exit_code, stdout, stderr = _run_codex(prompt, cwd, timeout, env)
    elif tool == "codex-review":
        if review_file:
            exit_code, stdout, stderr = _run_codex_review_file(prompt, cwd, timeout, env, review_file)
        else:
            exit_code, stdout, stderr = _run_codex_review(prompt, cwd, timeout, env, baseline_commit)
    else:
        exit_code, stdout, stderr = -2, "", f"Unknown tool: {tool}"

    end = datetime.now()
    duration_s = round((end - start).total_seconds(), 1)

    # Write log file
    safe_name = _safe_filename(task_id)
    log_path = os.path.join(log_dir, run_id, f"{safe_name}.log")
    _write_log(log_path, task_id, tool, prompt, snapshot, stdout, stderr, exit_code, duration_s)

    return RunResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        parsed=_try_parse_json(stdout),
        success=(exit_code == 0),
        log_file=log_path,
    )
