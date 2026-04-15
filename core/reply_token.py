"""File-backed reply token store with two-phase consume.

Token lifecycle: active -> reserved -> used
On failure:     reserved -> active  (release, user can retry)
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone, timedelta

TOKEN_TTL_MINUTES = 15
RESERVE_TTL_SECONDS = 120  # chat_reply timeout can be 60s+


class ReplyTokenStore:
    """File-backed token store with two-phase consume."""

    def __init__(self, path: str = "state/reply_tokens.json"):
        self.path = path
        self._lock = threading.Lock()

    def create(self, workspace: str, assistant_msg_id: str,
               assistant_reply: str) -> str:
        """Create token bound to assistant message ID with embedded reply snapshot."""
        token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        entry = {
            "workspace": workspace,
            "assistant_msg_id": assistant_msg_id,
            "assistant_reply": assistant_reply,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=TOKEN_TTL_MINUTES)).isoformat(),
            "status": "active",
            "reserved_at": None,
            "used_at": None,
        }
        with self._lock:
            data = self._read()
            data[token] = entry
            self._write(data)
        return token

    def peek(self, token: str, workspace_store) -> dict:
        """Read-only validation for GET (render reply page). Does not mutate."""
        with self._lock:
            data = self._read()
        entry = data.get(token)
        return self._validate(entry, workspace_store, check_reserved=False)

    def reserve(self, token: str, workspace_store) -> dict:
        """Atomic validate + mark reserved. Blocks concurrent submits.

        Returns {ok, reason, entry}. On success, token is reserved.
        """
        with self._lock:
            data = self._read()
            entry = data.get(token)
            result = self._validate(entry, workspace_store, check_reserved=True)
            if not result["ok"]:
                return result

            now = datetime.now(timezone.utc)
            entry["status"] = "reserved"
            entry["reserved_at"] = now.isoformat()
            self._write(data)

        return {"ok": True, "reason": "", "entry": entry}

    def finalize(self, token: str) -> None:
        """Mark reserved -> used (success path)."""
        with self._lock:
            data = self._read()
            entry = data.get(token)
            if entry and entry["status"] == "reserved":
                entry["status"] = "used"
                entry["used_at"] = datetime.now(timezone.utc).isoformat()
                self._write(data)

    def release(self, token: str) -> None:
        """Mark reserved -> active (failure path, user can retry)."""
        with self._lock:
            data = self._read()
            entry = data.get(token)
            if entry and entry["status"] == "reserved":
                entry["status"] = "active"
                entry["reserved_at"] = None
                self._write(data)

    def _validate(self, entry: dict | None, workspace_store,
                  check_reserved: bool) -> dict:
        """Common validation logic shared by peek/reserve."""
        if not entry:
            return {"ok": False, "reason": "not_found", "entry": None}

        status = entry.get("status", "active")

        if status == "used":
            return {"ok": False, "reason": "already_used", "entry": entry}

        if status == "reserved":
            if check_reserved:
                # Check if reservation expired (stale crash recovery)
                reserved_at = datetime.fromisoformat(entry["reserved_at"])
                now = datetime.now(timezone.utc)
                if (now - reserved_at).total_seconds() < RESERVE_TTL_SECONDS:
                    return {"ok": False, "reason": "processing", "entry": entry}
                # Stale reservation — auto-release, continue validation
            else:
                # peek: show "processing" status but don't block
                return {"ok": False, "reason": "processing", "entry": entry}

        now = datetime.now(timezone.utc)
        if now > datetime.fromisoformat(entry["expires_at"]):
            return {"ok": False, "reason": "expired", "entry": entry}

        # Conversation-moved: user msg after bound assistant msg?
        ws = workspace_store.get(entry["workspace"])
        if ws:
            target_id = entry["assistant_msg_id"]
            for msg in reversed(ws.get("messages", [])):
                if msg.get("id") == target_id:
                    break
                if msg.get("role") == "user":
                    return {"ok": False, "reason": "conversation_moved",
                            "entry": entry}

        return {"ok": True, "reason": "", "entry": entry}

    def _read(self) -> dict:
        if not os.path.isfile(self.path):
            return {}
        with open(self.path, "r") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
