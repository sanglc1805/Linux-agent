"""Read-only Linux diagnostic tools."""

from __future__ import annotations

import datetime as dt
import os
import platform
import re
import shutil
import socket
import urllib.parse
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..errors import ToolExecutionError, ValidationError
from ..runner import run
from ..runtime import Runtime
from ..security import (
    redact_secrets,
    safe_path,
    truncate_bytes,
    validate_container,
    validate_package,
    validate_unit,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _read_text(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def _parse_os_release() -> str:
    values: dict[str, str] = {}
    for line in _read_text("/etc/os-release").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values.get("PRETTY_NAME") or platform.platform()


def _meminfo() -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in _read_text("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1]) * 1024
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def _format_bytes(number: int) -> str:
    value = float(number)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def register(mcp: FastMCP, rt: Runtime) -> None:
    cfg = rt.config
    limit = cfg.max_output_bytes

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def system_overview() -> str:
        """Summarise host identity, kernel, uptime, CPU, memory and systemd state."""
        uptime_seconds = 0.0
        try:
            uptime_seconds = float(_read_text("/proc/uptime").split()[0])
        except (IndexError, ValueError):
            pass
        boot_time = dt.datetime.now() - dt.timedelta(seconds=uptime_seconds)
        load = _read_text("/proc/loadavg").split()[:3]
        mem_total, mem_available = _meminfo()
        systemd_state = run(["systemctl", "is-system-running"], timeout=5).output or "unknown"

        lines = [
            f"Host: {socket.gethostname()}",
            f"OS: {_parse_os_release()}",
            f"Kernel: {platform.release()} ({platform.machine()})",
            f"Python: {platform.python_version()}",
            f"Uptime: {uptime_seconds / 3600:.1f} hours (booted {boot_time:%Y-%m-%d %H:%M})",
            f"Load (1m, 5m, 15m): {', '.join(load) if load else 'unknown'}",
            f"CPU: {os.cpu_count()} logical cores",
            (
                f"Memory: {_format_bytes(mem_total - mem_available)} used of "
                f"{_format_bytes(mem_total)} ({_format_bytes(mem_available)} available)"
                if mem_total
                else "Memory: unknown"
            ),
            f"Systemd: {systemd_state}",
            f"User: {os.getenv('USER', str(os.getuid()))}",
        ]
        return truncate_bytes("\n".join(lines), limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def service_status(name: str) -> str:
        """Show systemctl status and recent lines for a systemd service (read-only)."""
        unit = validate_unit(name)
        result = run(
            ["systemctl", "status", unit, "--no-pager", "-l", "-n", "15"],
            timeout=cfg.read_timeout,
            max_bytes=limit,
        )
        header = f"Unit: {unit}\nExit code: {result.returncode}\n"
        return truncate_bytes(header + result.output, limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def service_logs(unit: str, lines: int = 50, since: str = "24 hours ago") -> str:
        """Read recent journal entries for a unit (read-only)."""
        unit = validate_unit(unit)
        lines = max(1, min(int(lines), 500))
        if not re.fullmatch(r"[0-9A-Za-z :._-]{1,40}", since or "") or since.startswith("-"):
            raise ValidationError("Invalid 'since' value. Example: '2 hours ago' or '2026-11-14'.")
        result = run(
            [
                "journalctl",
                "-u",
                unit,
                "-n",
                str(lines),
                "--since",
                since,
                "--no-pager",
                "--output=short-iso",
            ],
            timeout=cfg.read_timeout,
            max_bytes=limit,
        )
        if result.returncode != 0 and not result.output:
            raise ToolExecutionError(
                f"Cannot read journal for {unit}. Check journal permissions ({result.stderr})."
            )
        return truncate_bytes(result.output or "(no log entries)", limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def list_services(state: str = "running", limit: int = 40) -> str:
        """List systemd services filtered by state (read-only)."""
        allowed_states = {
            "running",
            "failed",
            "active",
            "inactive",
            "exited",
            "dead",
            "all",
        }
        state = (state or "running").lower()
        if state not in allowed_states:
            raise ValidationError(f"State must be one of: {', '.join(sorted(allowed_states))}.")
        count = max(1, min(int(limit), 200))
        argv = ["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend"]
        if state == "all":
            argv.append("--all")
        else:
            argv.append(f"--state={state}")
        result = run(argv, timeout=cfg.read_timeout, max_bytes=cfg.max_output_bytes)
        rows = [line for line in result.output.splitlines() if line.strip()]
        if not rows:
            return f"No {state} services found."
        return truncate_bytes("\n".join(rows[:count]), cfg.max_output_bytes)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def failed_units() -> str:
        """List failed systemd units with their descriptions (read-only)."""
        result = run(
            ["systemctl", "--failed", "--no-pager", "--no-legend"],
            timeout=cfg.read_timeout,
            max_bytes=limit,
        )
        return truncate_bytes(result.output or "No failed units.", limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def disk_usage() -> str:
        """Show filesystem usage in human-readable form (read-only)."""
        result = run(
            ["df", "-hT", "-x", "squashfs"],
            timeout=cfg.read_timeout,
            max_bytes=limit,
        )
        return truncate_bytes(result.output, limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def process_top(sort_by: str = "cpu", limit: int = 10) -> str:
        """Show the top processes by CPU or memory (read-only)."""
        sort_by = (sort_by or "cpu").lower()
        if sort_by not in {"cpu", "mem"}:
            raise ValidationError("sort_by must be 'cpu' or 'mem'.")
        count = max(1, min(int(limit), 50))
        result = run(
            [
                "ps",
                "-eo",
                "pid,user,pcpu,pmem,rss,etimes,comm",
                "--sort=-%" + ("cpu" if sort_by == "cpu" else "mem"),
            ],
            timeout=cfg.read_timeout,
            max_bytes=cfg.max_output_bytes,
        )
        rows = [line for line in result.output.splitlines() if line.strip()]
        return truncate_bytes("\n".join(rows[: count + 1]), cfg.max_output_bytes)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def network_overview() -> str:
        """Show interface addresses, routes and DNS resolvers (read-only)."""
        addresses = run(["ip", "-brief", "address"], timeout=cfg.read_timeout).output
        routes = run(["ip", "route"], timeout=cfg.read_timeout).output
        resolvers = [
            line.strip()
            for line in _read_text("/etc/resolv.conf").splitlines()
            if line.strip().startswith("nameserver")
        ]
        text = (
            "Interfaces:\n"
            + (addresses or "(unavailable)")
            + "\n\nRoutes:\n"
            + (routes or "(unavailable)")
            + "\n\nDNS:\n"
            + ("\n".join(resolvers) if resolvers else "(none)")
        )
        return truncate_bytes(redact_secrets(text), limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def listening_ports() -> str:
        """List listening TCP/UDP sockets and owning processes (read-only)."""
        result = run(
            ["ss", "-tulpn"],
            timeout=cfg.read_timeout,
            max_bytes=limit,
        )
        if result.returncode != 0:
            result = run(["ss", "-tuln"], timeout=cfg.read_timeout, max_bytes=limit)
        return truncate_bytes(result.output, limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def http_probe(url: str) -> str:
        """Check a local HTTP(S) endpoint and show status, headers and a body preview."""
        if len(url) > 2048:
            raise ValidationError("URL is too long.")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValidationError("URL must start with http:// or https://.")
        if parsed.username or parsed.password:
            raise ValidationError("Credentials in URLs are not allowed.")
        host = (parsed.hostname or "").lower()
        if host not in cfg.http_allowed_hosts:
            allowed = ", ".join(sorted(cfg.http_allowed_hosts))
            raise ValidationError(f"Host {host!r} is not allowed. Allowed hosts: {allowed}.")
        try:
            with httpx.Client(
                follow_redirects=False,
                timeout=10.0,
                trust_env=False,
            ) as client:
                response = client.get(url, headers={"User-Agent": "linux-agent/1.0"})
            body = response.text[:2000] if response.content else ""
            headers = "\n".join(
                f"{key}: {value}"
                for key, value in response.headers.items()
                if key.lower() not in {"set-cookie"}
            )
            text = (
                f"URL: {url}\n"
                f"Status: {response.status_code} {response.reason_phrase}\n"
                f"Latency: {response.elapsed.total_seconds() * 1000:.1f} ms\n"
                f"Content-Type: {response.headers.get('content-type', 'unknown')}\n"
                f"Content-Length: {len(response.content)}\n\n"
                f"Headers:\n{headers}\n\nBody preview:\n{body or '(empty)'}"
            )
            return truncate_bytes(redact_secrets(text), limit)
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"HTTP probe failed for {url}: {exc}") from exc

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def package_info(name: str) -> str:
        """Show installed version and APT policy for a package (read-only)."""
        package = validate_package(name)
        installed = run(
            [
                "dpkg-query",
                "-W",
                "-f=${Package}\\t${Version}\\t${Status}\\t${Installed-Size} KiB\\n",
                package,
            ],
            timeout=cfg.read_timeout,
        )
        policy = run(["apt-cache", "policy", package], timeout=cfg.read_timeout)
        text = (
            f"Installed:\n{installed.output or '(not installed)'}\n\n"
            f"APT policy:\n{policy.output or '(unavailable)'}"
        )
        return truncate_bytes(text, limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def read_config(path: str, max_lines: int = 200) -> str:
        """Read a configuration file below an allowed root, with secrets redacted."""
        resolved = safe_path(path, cfg.file_roots, base=cfg.root)
        if not resolved.is_file():
            raise ToolExecutionError(f"Not a regular file: {resolved}")
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise ToolExecutionError(f"Cannot stat {resolved}: {exc}") from exc
        if size > 2 * 1024 * 1024:
            raise ToolExecutionError("File is larger than 2 MiB; refusing to read it.")
        max_lines = max(1, min(int(max_lines), cfg.max_config_lines))
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolExecutionError(f"Cannot read {resolved}: {exc}") from exc
        lines = text.splitlines()
        shown = "\n".join(f"{index:4d}: {line}" for index, line in enumerate(lines[:max_lines], 1))
        suffix = f"\n... ({len(lines) - max_lines} more lines)" if len(lines) > max_lines else ""
        return truncate_bytes(
            redact_secrets(f"File: {resolved}\n\n{shown}{suffix}"),
            limit,
        )

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def container_list(all_containers: bool = True) -> str:
        """List Docker containers and their status (read-only)."""
        if not shutil.which("docker"):
            return "Docker is not installed on this host."
        argv = ["docker", "ps", "--format", "{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}"]
        if all_containers:
            argv.insert(2, "-a")
        result = run(argv, timeout=cfg.read_timeout, max_bytes=limit)
        if result.returncode != 0:
            return f"Docker is unavailable or permission was denied:\n{result.output}"
        return truncate_bytes(result.output or "No containers.", limit)

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def container_logs(name: str, lines: int = 50) -> str:
        """Read the last N log lines from a Docker container (read-only)."""
        name = validate_container(name)
        lines = max(1, min(int(lines), 500))
        if not shutil.which("docker"):
            return "Docker is not installed on this host."
        result = run(
            ["docker", "logs", "--tail", str(lines), "--timestamps", name],
            timeout=cfg.read_timeout,
            max_bytes=limit,
        )
        if result.returncode != 0:
            raise ToolExecutionError(f"Cannot read logs for container {name}: {result.output}")
        return truncate_bytes(result.output or "(no logs)", limit)

    @mcp.resource("linux://system/summary", mime_type="text/plain")
    def system_summary_resource() -> str:
        """Live host summary exposed as an MCP resource."""
        return system_overview()

    @mcp.resource("linux://logs/{unit}", mime_type="text/plain")
    def unit_logs_resource(unit: str) -> str:
        """Last 50 journal lines for a unit, exposed as an MCP resource."""
        return service_logs(unit, lines=50, since="24 hours ago")
