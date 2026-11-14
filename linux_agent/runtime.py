"""Shared runtime object used by all MCP tools."""

from __future__ import annotations

import functools
import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .audit import AuditLog
from .config import Config
from .errors import PolicyError
from .security import redact_obj


@dataclass
class Runtime:
    config: Config
    audit: AuditLog
    _write_lock: threading.Lock = field(default_factory=threading.Lock)
    _last_write: float = 0.0

    def require_write(self, action: str) -> None:
        if not self.config.allow_write:
            raise PolicyError(
                f"Write operation '{action}' is disabled. Start the server with "
                "LINUX_AGENT_ALLOW_WRITE=1 (and install the privileged helper) "
                "to enable changes."
            )

    def throttle_write(self) -> None:
        """Serialize write operations and enforce a minimum interval."""
        with self._write_lock:
            wait = self.config.min_write_interval - (time.monotonic() - self._last_write)
            if wait > 0:
                time.sleep(wait)
            self._last_write = time.monotonic()

    def audited(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator that records every MCP tool call in the audit log."""

        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                tool_args = redact_obj(dict(bound.arguments))
            except TypeError:
                tool_args = {"args": redact_obj(list(args)), "kwargs": redact_obj(kwargs)}

            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                self.audit.record(
                    "tool_call",
                    tool=fn.__name__,
                    args=tool_args,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            self.audit.record(
                "tool_call",
                tool=fn.__name__,
                args=tool_args,
                ok=True,
                result=str(result),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return result

        return wrapper
