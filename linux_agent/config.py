"""Policy and configuration loading."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .security import unit_base, validate_unit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "policy.toml"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class NginxParameter:
    name: str
    context: str
    pattern: str
    description: str

    def validate(self, value: str) -> str:
        """Validate a value, including semantic bounds for known parameters."""
        if not isinstance(value, str):
            raise ValidationError("Parameter value must be a string.")
        value = value.strip()
        if not value:
            raise ValidationError("Parameter value must not be empty.")
        if len(value) > 32:
            raise ValidationError("Parameter value is too long (max 32 characters).")
        if not re.fullmatch(self.pattern, value):
            raise ValidationError(
                f"Value {value!r} does not match the allowed pattern {self.pattern!r} "
                f"for {self.name}."
            )

        lower = value.lower()
        if self.name == "worker_processes" and lower != "auto":
            number = int(value)
            if not 1 <= number <= 64:
                raise ValidationError("worker_processes must be auto or between 1 and 64.")
        elif self.name == "worker_connections":
            number = int(value)
            if not 128 <= number <= 65535:
                raise ValidationError("worker_connections must be between 128 and 65535.")
        elif self.name in {"keepalive_timeout"}:
            if not 0 <= int(value) <= 600:
                raise ValidationError(f"{self.name} must be between 0 and 600 seconds.")
        elif self.name in {"client_body_timeout", "send_timeout"}:
            if not 0 <= int(value) <= 300:
                raise ValidationError(f"{self.name} must be between 0 and 300 seconds.")
        elif self.name == "client_max_body_size" and lower != "0":
            number = int(re.sub(r"[kmg]$", "", lower))
            unit = lower[-1] if lower[-1] in "kmg" else ""
            multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit]
            if number * multiplier > 10 * 1024**3:
                raise ValidationError("client_max_body_size may not exceed 10g.")
        return value


@dataclass
class Config:
    root: Path
    path: Path
    raw: dict[str, Any]
    allow_write: bool
    audit_log: Path
    max_output_bytes: int
    read_timeout: float
    write_timeout: float
    min_write_interval: float
    nginx_parameters: dict[str, NginxParameter] = field(default_factory=dict)

    # ---------------------------------------------------------------- helpers
    @property
    def server_name(self) -> str:
        return str(self.raw.get("server", {}).get("name", "linux-agent"))

    @property
    def instructions(self) -> str:
        return str(self.raw.get("server", {}).get("instructions", "")).strip()

    @property
    def allowed_units(self) -> set[str]:
        units = self.raw.get("units", {}).get("allowed", [])
        return {unit_base(str(u)) for u in units}

    @property
    def allowed_actions(self) -> set[str]:
        return {
            str(a).lower()
            for a in self.raw.get("units", {}).get("actions", [])
        }

    def is_unit_allowed(self, name: str) -> bool:
        return unit_base(validate_unit(name)) in self.allowed_units

    def is_action_allowed(self, action: str) -> bool:
        return action in self.allowed_actions

    def nginx_parameter(self, name: str) -> NginxParameter:
        try:
            return self.nginx_parameters[name]
        except KeyError as exc:
            allowed = ", ".join(sorted(self.nginx_parameters))
            raise ValidationError(
                f"Unknown nginx parameter {name!r}. Allowed: {allowed}."
            ) from exc

    @property
    def file_roots(self) -> list[Path]:
        roots = self.raw.get("files", {}).get("allowed_roots", [])
        result: list[Path] = []
        for root in roots:
            path = Path(str(root)).expanduser()
            result.append(path if path.is_absolute() else (self.root / path))
        return [p.resolve(strict=False) for p in result]

    @property
    def max_config_lines(self) -> int:
        return int(self.raw.get("files", {}).get("max_lines", 400))

    @property
    def http_allowed_hosts(self) -> set[str]:
        hosts = self.raw.get("network", {}).get("allowed_hosts", [])
        return {str(h).lower() for h in hosts}

    @property
    def use_sudo(self) -> bool:
        default = bool(self.raw.get("helpers", {}).get("use_sudo", True))
        return _env_bool("LINUX_AGENT_USE_SUDO", default)

    @property
    def nginx_unit(self) -> str:
        return validate_unit(str(self.raw.get("nginx", {}).get("unit", "nginx")))

    def helper(self, kind: str) -> Path:
        """Return the path of a privileged helper.

        Resolution order:
        1. LINUX_AGENT_<KIND>_HELPER (explicit file)
        2. LINUX_AGENT_HELPER_DIR/<name>
        3. Installed root-owned helper, if present and preferred
        4. Repository development copy
        """
        if kind not in {"nginx", "service"}:
            raise ValueError(f"Unknown helper kind: {kind}")

        explicit = os.environ.get(f"LINUX_AGENT_{kind.upper()}_HELPER")
        if explicit:
            return Path(explicit).expanduser().resolve(strict=False)

        helpers = self.raw.get("helpers", {})
        filename = str(helpers.get(kind, f"{kind}.sh"))
        directory = os.environ.get("LINUX_AGENT_HELPER_DIR")
        if directory:
            return (Path(directory).expanduser() / filename).resolve(strict=False)

        installed = Path(str(helpers.get("dir", "/usr/local/libexec/linux-agent")))
        installed_path = installed / filename
        preferred = bool(self.raw.get("nginx", {}).get("prefer_installed", True))
        if preferred and installed_path.is_file():
            return installed_path

        if kind == "nginx":
            repo = self.raw.get("nginx", {}).get("repo_copy", "scripts/nginx_param.sh")
            return (self.root / str(repo)).resolve(strict=False)
        return (self.root / "scripts" / filename).resolve(strict=False)

    @property
    def agent_settings(self) -> dict[str, Any]:
        settings = dict(self.raw.get("agent", {}))
        settings["model"] = os.environ.get("GROQ_MODEL", settings.get("model", "openai/gpt-oss-20b"))
        settings["max_steps"] = _env_int("LINUX_AGENT_MAX_STEPS", int(settings.get("max_steps", 8)))
        settings["max_tool_output_chars"] = _env_int(
            "LINUX_AGENT_MAX_TOOL_OUTPUT",
            int(settings.get("max_tool_output_chars", 12000)),
        )
        return settings


def load_config(path: str | Path | None = None) -> Config:
    """Load policy.toml and apply environment overrides."""
    policy_path = Path(
        path or os.environ.get("LINUX_AGENT_POLICY", DEFAULT_POLICY)
    ).expanduser()
    if not policy_path.is_file():
        raise FileNotFoundError(f"Policy file not found: {policy_path}")

    with policy_path.open("rb") as handle:
        raw = tomllib.load(handle)

    security = raw.get("security", {})
    raw_allow = bool(security.get("allow_write", False))
    allow_write = _env_bool("LINUX_AGENT_ALLOW_WRITE", raw_allow)

    audit_raw = os.environ.get(
        "LINUX_AGENT_AUDIT_LOG", str(security.get("audit_log", "logs/audit.jsonl"))
    )
    audit_path = Path(audit_raw).expanduser()
    if not audit_path.is_absolute():
        audit_path = ROOT / audit_path

    parameters: dict[str, NginxParameter] = {}
    for name, spec in raw.get("nginx", {}).get("parameters", {}).items():
        parameters[name] = NginxParameter(
            name=name,
            context=str(spec.get("context", "http")),
            pattern=str(spec.get("pattern", ".*")),
            description=str(spec.get("description", "")),
        )

    return Config(
        root=ROOT,
        path=policy_path.resolve(),
        raw=raw,
        allow_write=allow_write,
        audit_log=audit_path.resolve(strict=False),
        max_output_bytes=_env_int(
            "LINUX_AGENT_MAX_OUTPUT_BYTES", int(security.get("max_output_bytes", 24000))
        ),
        read_timeout=float(
            os.environ.get(
                "LINUX_AGENT_READ_TIMEOUT",
                security.get("read_timeout_seconds", 20),
            )
        ),
        write_timeout=float(
            os.environ.get(
                "LINUX_AGENT_WRITE_TIMEOUT",
                security.get("write_timeout_seconds", 60),
            )
        ),
        min_write_interval=float(
            os.environ.get(
                "LINUX_AGENT_MIN_WRITE_INTERVAL",
                security.get("min_write_interval_seconds", 1.0),
            )
        ),
        nginx_parameters=parameters,
    )
