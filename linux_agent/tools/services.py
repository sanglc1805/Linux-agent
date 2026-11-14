"""Controlled service actions."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..errors import PolicyError, ToolExecutionError
from ..runner import run, run_privileged
from ..runtime import Runtime
from ..security import validate_action, validate_unit

WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def register(mcp: FastMCP, rt: Runtime) -> None:
    cfg = rt.config

    @mcp.tool(annotations=WRITE)
    @rt.audited
    def service_action(unit: str, action: str, dry_run: bool = True) -> str:
        """Start/stop/restart/reload an allow-listed systemd unit.

        dry_run defaults to true: the call only describes what would happen.
        Set dry_run=false to execute the action through the privileged helper.
        """
        unit = validate_unit(unit)
        action = validate_action(action)

        if not cfg.is_unit_allowed(unit):
            raise PolicyError(
                f"Unit {unit!r} is not allow-listed for changes. Allowed units: "
                f"{', '.join(sorted(cfg.allowed_units)) or '(none)'}."
            )
        if not cfg.is_action_allowed(action):
            raise PolicyError(
                f"Action {action!r} is not allowed. Allowed actions: "
                f"{', '.join(sorted(cfg.allowed_actions)) or '(none)'}."
            )

        target = unit if unit.endswith(".service") else f"{unit}.service"
        helper = cfg.helper("service")
        command = (
            f"sudo -n -- {helper} {action} {target}"
            if cfg.use_sudo
            else f"{helper} {action} {target}"
        )

        if dry_run:
            return (
                f"DRY RUN: would execute:\n  {command}\n"
                f"Policy: unit '{target}' and action '{action}' are allow-listed.\n"
                "No change was made. Call again with dry_run=false to apply."
            )

        rt.require_write(f"service_action:{action}:{target}")
        rt.throttle_write()

        result = run_privileged(
            helper,
            [action, target],
            use_sudo=cfg.use_sudo,
            timeout=cfg.write_timeout,
            max_bytes=cfg.max_output_bytes,
        )
        if not result.ok:
            raise ToolExecutionError(
                f"Service action failed (exit {result.returncode}):\n{result.output}"
            )

        status = run(
            ["systemctl", "is-active", target],
            timeout=cfg.read_timeout,
        ).output
        recent = run(
            ["journalctl", "-u", target, "-n", "8", "--no-pager", "--output=short-iso"],
            timeout=cfg.read_timeout,
            max_bytes=6000,
        ).output
        return (
            f"OK: {action} {target}\n"
            f"State: {status or 'unknown'}\n"
            f"Helper output:\n{result.output or '(none)'}\n\n"
            f"Recent journal:\n{recent or '(none)'}"
        )
