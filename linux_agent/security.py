"""Input validation, redaction and path confinement.

The MCP server is deliberately conservative: every value that reaches a
privileged helper is validated here first. Never rely on the language model to
produce safe input.
"""

from __future__ import annotations

import re
from pathlib import Path

from .errors import ValidationError

# A systemd unit name: letters/digits followed by a conservative set. We
# explicitly reject a leading '-' (systemctl option injection) and '..'.
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,127}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
BACKUP_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9]+$")

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|"
            r"api[_-]?key|access[_-]?key))"
            r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
        r"\1=<redacted>",
    ),
    (
        re.compile(r"(?i)\b(authorization)\s*:\s*(?:bearer|basic)\s+\S+"),
        r"\1: <redacted>",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED PRIVATE KEY]",
    ),
)


def validate_unit(name: str) -> str:
    """Validate and return a systemd unit name (without changing it)."""
    if not isinstance(name, str):
        raise ValidationError("Unit name must be a string.")
    name = name.strip()
    if not UNIT_RE.fullmatch(name):
        raise ValidationError(
            f"Invalid unit name {name!r}. Use a plain systemd unit name such as nginx "
            "or nginx.service."
        )
    if ".." in name:
        raise ValidationError("Invalid unit name: '..' is not allowed.")
    return name


def unit_base(name: str) -> str:
    """Return the unit name without a trailing .service suffix."""
    name = validate_unit(name)
    return name[: -len(".service")] if name.endswith(".service") else name


def validate_action(action: str) -> str:
    action = (action or "").strip().lower()
    if action not in {"start", "stop", "restart", "reload"}:
        raise ValidationError(
            "Action must be one of: start, stop, restart, reload."
        )
    return action


def validate_param_name(name: str) -> str:
    name = (name or "").strip()
    if not PARAM_RE.fullmatch(name):
        raise ValidationError(f"Invalid parameter name {name!r}.")
    return name


def validate_backup_id(backup_id: str) -> str:
    backup_id = (backup_id or "").strip()
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise ValidationError(f"Invalid backup id {backup_id!r}.")
    return backup_id


def truncate_bytes(text: str, limit: int, note: str = "\n...[output truncated]") -> str:
    """Truncate a string to roughly *limit* UTF-8 bytes without splitting a char."""
    if limit <= 0:
        return ""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    cut = data[: max(0, limit - len(note.encode()))].decode("utf-8", errors="ignore")
    return cut + note


def redact_secrets(text: str) -> str:
    """Redact common secret assignments and private keys from text."""
    if not text:
        return text
    result = text
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_obj(value: object, depth: int = 0) -> object:
    """Recursively redact strings inside JSON-like values."""
    if depth > 8:
        return "<depth-limited>"
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {str(k): redact_obj(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, depth + 1) for v in value]
    return value


def safe_path(raw: str, roots: list[Path], base: Path | None = None) -> Path:
    """Resolve *raw* and require it to live below one of *roots*."""
    if not raw or "\x00" in raw:
        raise ValidationError("A non-empty file path is required.")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    resolved = candidate.resolve(strict=False)
    for root in roots:
        root_resolved = root.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            continue
        return resolved
    allowed = ", ".join(str(r) for r in roots) or "(none)"
    raise ValidationError(f"Path {raw!r} is outside the allowed roots: {allowed}.")


def validate_package(name: str) -> str:
    name = (name or "").strip()
    if not PACKAGE_RE.fullmatch(name):
        raise ValidationError(f"Invalid package name {name!r}.")
    return name


def validate_container(name: str) -> str:
    name = (name or "").strip()
    if not CONTAINER_RE.fullmatch(name):
        raise ValidationError(f"Invalid container name {name!r}.")
    return name
