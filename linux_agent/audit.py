"""Append-only JSONL audit log."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .security import redact_obj, truncate_bytes


class AuditLog:
    """Thread-safe JSONL writer that never crashes a tool on logging errors."""

    def __init__(self, path: Path, max_preview: int = 2000) -> None:
        self.path = path
        self.max_preview = max_preview
        self._lock = threading.Lock()
        self._warned = False

    def record(self, event: str, **fields: Any) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "pid": os.getpid(),
            "user": _current_user(),
        }
        for key, value in fields.items():
            if key in {"args", "result", "error", "detail"}:
                value = redact_obj(value)
            if isinstance(value, str) and len(value) > self.max_preview:
                value = truncate_bytes(value, self.max_preview, "...[audit preview truncated]")
            entry[key] = value

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError as exc:
            if not self._warned:
                print(f"[audit] cannot write {self.path}: {exc}", file=sys.stderr)
                self._warned = True


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return str(os.getuid())
