"""Nginx inspection and controlled configuration changes."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..errors import ToolExecutionError
from ..runner import CommandResult, run, run_privileged
from ..runtime import Runtime
from ..security import validate_backup_id, validate_param_name

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)


def register(mcp: FastMCP, rt: Runtime) -> None:
    cfg = rt.config

    def _call(args: list[str], *, privileged: bool = False) -> CommandResult:
        helper = cfg.helper("nginx")
        if privileged and cfg.use_sudo:
            return run_privileged(
                helper,
                args,
                use_sudo=True,
                timeout=cfg.write_timeout,
                max_bytes=cfg.max_output_bytes,
            )
        return run(
            [str(helper), *args],
            timeout=cfg.write_timeout,
            max_bytes=cfg.max_output_bytes,
        )

    def _read(args: list[str]) -> CommandResult:
        """Try the helper unprivileged, then with sudo for root-only files."""
        result = _call(args)
        if result.ok:
            return result
        if cfg.use_sudo:
            fallback = _call(args, privileged=True)
            lowered = fallback.output.lower()
            sudo_blocked = "password is required" in lowered or "interactive authentication" in lowered
            if fallback.ok or not sudo_blocked:
                return fallback
        return result

    def _ensure_ok(result: CommandResult, label: str) -> str:
        if not result.ok:
            raise ToolExecutionError(f"{label} failed (exit {result.returncode}):\n{result.output}")
        return result.output or "(no output)"

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def nginx_info() -> str:
        """Show nginx version, configuration test result and current unit state."""
        version = run(["nginx", "-v"], timeout=cfg.read_timeout)
        test = _read(["test"])
        state = run(["systemctl", "is-active", cfg.nginx_unit], timeout=cfg.read_timeout)
        return (
            f"Version: {version.output or '(nginx not found)'}\n"
            f"Unit {cfg.nginx_unit}: {state.output or 'unknown'}\n\n"
            f"nginx -t:\n{test.output or '(no output)'}"
        )

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def nginx_list_parameters() -> str:
        """List managed nginx parameters and their current configured values."""
        return _ensure_ok(_read(["list"]), "Listing nginx parameters")

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def nginx_get_parameter(parameter: str) -> str:
        """Read one managed nginx parameter from the current configuration."""
        parameter = validate_param_name(parameter)
        spec = cfg.nginx_parameter(parameter)
        output = _ensure_ok(_read(["get", parameter]), f"Reading {parameter}")
        return f"Parameter: {parameter}\nContext: {spec.context}\nDescription: {spec.description}\nValue: {output}"

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def nginx_list_backups() -> str:
        """List configuration backups created by the nginx helper."""
        return _ensure_ok(_read(["list-backups"]), "Listing nginx backups")

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def nginx_diff_backup(backup_id: str) -> str:
        """Show the unified diff recorded in an nginx configuration backup."""
        backup_id = validate_backup_id(backup_id)
        return _ensure_ok(_read(["diff-backup", backup_id]), "Reading backup diff")

    @mcp.tool(annotations=READ_ONLY)
    @rt.audited
    def nginx_test_config() -> str:
        """Run nginx -t through the helper and return the validation output."""
        return _ensure_ok(_read(["test"]), "Testing nginx configuration")

    @mcp.tool(annotations=WRITE)
    @rt.audited
    def nginx_set_parameter(
        parameter: str,
        value: str,
        reload: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Safely change one allow-listed nginx parameter.

        The helper validates the value, creates a timestamped backup, writes the
        new configuration, runs nginx -t and rolls back automatically on failure.
        dry_run defaults to true and only returns a diff. Set dry_run=false to
        apply. Set reload=true to reload nginx after a successful test.
        """
        parameter = validate_param_name(parameter)
        spec = cfg.nginx_parameter(parameter)
        value = spec.validate(value)

        args = ["set", parameter, value]
        if dry_run:
            args.append("--dry-run")
        if reload and not dry_run:
            args.append("--reload")

        if not dry_run:
            rt.require_write(f"nginx_set_parameter:{parameter}")
            rt.throttle_write()
            result = _call(args, privileged=True)
        else:
            result = _call(args)

        output = _ensure_ok(result, f"Setting {parameter}={value}")
        prefix = "DRY RUN" if dry_run else "APPLIED"
        return (
            f"{prefix}: {parameter} = {value} (context: {spec.context})\n"
            f"Reload requested: {'yes' if reload and not dry_run else 'no'}\n\n{output}"
        )

    @mcp.tool(annotations=WRITE)
    @rt.audited
    def nginx_rollback(
        backup_id: str,
        reload: bool = False,
        dry_run: bool = True,
    ) -> str:
        """Restore an nginx configuration backup, validating before reload.

        A safety backup of the current state is created first. dry_run defaults
        to true and only shows the diff.
        """
        backup_id = validate_backup_id(backup_id)
        args = ["rollback", backup_id]
        if dry_run:
            args.append("--dry-run")
        if reload and not dry_run:
            args.append("--reload")

        if not dry_run:
            rt.require_write(f"nginx_rollback:{backup_id}")
            rt.throttle_write()
            result = _call(args, privileged=True)
        else:
            result = _call(args)

        output = _ensure_ok(result, f"Rolling back {backup_id}")
        prefix = "DRY RUN" if dry_run else "ROLLED BACK"
        return f"{prefix}: {backup_id}\n\n{output}"

    @mcp.resource("linux://nginx/parameters", mime_type="text/plain")
    def nginx_parameters_resource() -> str:
        """Current values of the managed nginx parameters."""
        return nginx_list_parameters()
