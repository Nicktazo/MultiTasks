"""Lightweight event logger — append-only JSONL per run."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class EventRecord:
    ts: str
    type: str
    run_id: str
    task_id: str | None = None
    status: str | None = None
    message: str = ""
    data: dict | None = None


class EventLogger:
    """Thread-safe append-only event logger writing to <run_id>.events.jsonl."""

    def __init__(self, state_dir: str, run_id: str) -> None:
        self.state_dir = state_dir
        self.run_id = run_id
        self._lock = threading.Lock()
        self._path = os.path.join(state_dir, f"{run_id}.events.jsonl")

    @property
    def path(self) -> str:
        return self._path

    def log(self, type: str, task_id: str | None = None,
            status: str | None = None, message: str = "",
            data: dict | None = None) -> None:
        """Append one event. Swallows errors to avoid breaking runs."""
        record = EventRecord(
            ts=datetime.now().isoformat(),
            type=type,
            run_id=self.run_id,
            task_id=task_id,
            status=status,
            message=message,
            data=data,
        )
        line = json.dumps(asdict(record), ensure_ascii=False) + "\n"
        try:
            with self._lock:
                os.makedirs(self.state_dir, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
        except Exception as e:
            print(f"[events] log error: {e}", file=sys.stderr)

    def read_all(self) -> list[dict]:
        """Read all events. Returns empty list if file missing or corrupt lines."""
        if not os.path.isfile(self._path):
            return []
        events = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # skip corrupt lines
        except OSError:
            return []
        return events
