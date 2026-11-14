"""Subprocess execution without a shell."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

from .errors import ToolExecutionError
from .security import truncate_bytes

SAFE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "SYSTEMD_PAGER": "cat",
    "SYSTEMD_COLORS": "0",
    "PAGER": "cat",
}


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        text = self.stdout
        if self.stderr:
            text = f"{text}\n{self.stderr}" if text else self.stderr
        return text.strip()

    def as_text(self, limit: int = 24000) -> str:
        return truncate_bytes(self.output, limit)


def run(
    argv: list[str],
    timeout: float = 20,
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    max_bytes: int = 24000,
) -> CommandResult:
    """Run a command without a shell, killing the whole process group on timeout."""
    if not argv or not all(isinstance(part, str) for part in argv):
        raise ToolExecutionError("Command must be a non-empty list of strings.")

    child_env = dict(SAFE_ENV)
    if env:
        child_env.update({str(k): str(v) for k, v in env.items()})

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=child_env,
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"Command not found: {argv[0]}") from exc
    except OSError as exc:
        raise ToolExecutionError(f"Cannot execute {argv[0]}: {exc}") from exc

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        stdout, stderr = process.communicate()
        stderr = (stderr or "") + f"\nCommand timed out after {timeout:g}s."

    duration_ms = int((time.monotonic() - started) * 1000)
    return CommandResult(
        argv=argv,
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=truncate_bytes(stdout or "", max_bytes),
        stderr=truncate_bytes(stderr or "", max_bytes),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


def run_privileged(
    helper,
    args: list[str],
    *,
    use_sudo: bool,
    timeout: float = 60,
    max_bytes: int = 24000,
) -> CommandResult:
    """Invoke an installed helper, normally through non-interactive sudo."""
    helper_path = str(helper)
    if not os.path.isfile(helper_path):
        raise ToolExecutionError(
            f"Privileged helper not found: {helper_path}. "
            "Run scripts/install_privileged.sh once as root, or set "
            "LINUX_AGENT_NGINX_HELPER / LINUX_AGENT_SERVICE_HELPER."
        )
    argv = ["sudo", "-n", "--", helper_path, *args] if use_sudo else [helper_path, *args]
    result = run(argv, timeout=timeout, max_bytes=max_bytes)
    lowered = result.output.lower()
    if result.returncode != 0 and (
        "password is required" in lowered or "interactive authentication" in lowered
    ):
        raise ToolExecutionError(
            "sudo requires a password. Install the sudoers rule with "
            "scripts/install_privileged.sh or run the server as root."
        )
    return result
