"""Explicit two-step cleanup of terminal records. Never touches media."""
from datetime import datetime, timedelta, timezone
import secrets
import threading
import time
from .store import TERMINAL_STATUSES


class Maintenance:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.previews = {}

    def preview(self, payload):
        if set(payload) - {"statuses", "days"}:
            raise ValueError("unknown cleanup field")
        statuses, days = payload.get("statuses", ["succeeded", "canceled"]), payload.get("days", 30)
        if not isinstance(statuses, list) or not statuses or any(not isinstance(s, str) or s not in TERMINAL_STATUSES for s in statuses):
            raise ValueError("only terminal statuses can be cleaned")
        if type(days) is not int or days not in (7, 30, 90):
            raise ValueError("cleanup age must be 7, 30 or 90 days")
        before = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.store.cleanup_candidates(set(statuses), before)
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.previews = {k: v for k, v in self.previews.items() if v[0] > time.monotonic()}
            if len(self.previews) >= 100:
                self.previews.pop(next(iter(self.previews)))
            self.previews[token] = (time.monotonic() + 300, rows, set(statuses), before)
        return {"token": token, "count": len(rows), "candidates": rows[:20], "expires_seconds": 300, "limit": 1000}

    def cleanup(self, payload):
        if set(payload) != {"token"} or not isinstance(payload["token"], str):
            raise ValueError("cleanup requires the preview token")
        with self.lock:
            preview = self.previews.pop(payload["token"], None)
        if not preview or preview[0] <= time.monotonic():
            raise ValueError("cleanup preview expired; preview again")
        _, rows, statuses, before = preview
        # Match the old terminal timestamp too: a retried task may have completed
        # again between preview and confirmation.
        current = {r["id"]: r for r in self.store.cleanup_candidates(statuses, before, 100000)}
        eligible = [r for r in rows if current.get(r["id"]) == r]
        result = self.store.cleanup_jobs(eligible, statuses, before)
        result["skipped"] += len(rows) - len(eligible)
        return result
