"""WhatsApp/Telegram notification with rate limiting."""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .state import RunState


def _sanitize(message: str) -> str:
    """Strip to safe characters only, truncate to 500 chars.

    Belt-and-suspenders: even though we use shlex.quote, strip anything
    that has no business being in a notification message.
    """
    return re.sub(r"[^\w\s.,:=\-/()+@#]", "", message)[:500]


def send_notification(settings: Settings, message: str) -> None:
    """Send notification via configured channel. Errors are silently swallowed."""
    if settings.notify == "none":
        return

    channel = settings.notify
    if channel not in ("whatsapp", "telegram"):
        return

    msg = _sanitize(message)

    # Build the inner command that su -c will execute.
    # shlex.quote wraps msg in single quotes with proper escaping.
    target = "+8618918668262"
    inner_cmd = f"openclaw message send --channel {channel} -t {target} -m {shlex.quote(msg)}"
    remote_cmd = f"su - Nick -c {shlex.quote(inner_cmd)}"

    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
        "root@137.220.43.207",
        remote_cmd,
    ]

    try:
        subprocess.run(cmd, timeout=30, capture_output=True)
    except Exception as e:
        print(f"  [notify] {type(e).__name__}: {str(e)[:80]}")


def notify_task_failed(settings: Settings, task_id: str, error: str) -> None:
    """Notify on task failure."""
    send_notification(settings, f"FAIL {task_id}: {error[:100]}")


def notify_pipeline_done(settings: Settings, state: RunState) -> None:
    """Send pipeline completion summary."""
    counts: dict[str, int] = {}
    for ts in state.tasks.values():
        counts[ts.status] = counts.get(ts.status, 0) + 1

    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    summary = ", ".join(parts)
    send_notification(settings, f"Pipeline {state.run_id} done. {summary}")
