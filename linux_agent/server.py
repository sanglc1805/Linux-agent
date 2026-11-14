"""MCP server entry point."""

from __future__ import annotations

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP

from .audit import AuditLog
from .config import Config, load_config
from .runtime import Runtime
from .tools import diagnostics, nginx, services

DEFAULT_INSTRUCTIONS = """
You are connected to the Linux Agent MCP server. Use read-only tools to inspect
the host before proposing changes. Mutating tools are allow-listed, dry-run by
default, audited, and may require human approval in the client. Never invent
command output. Report errors honestly.
""".strip()


def create_server(config: Config | None = None) -> FastMCP:
    cfg = config or load_config()
    audit = AuditLog(cfg.audit_log)
    runtime = Runtime(cfg, audit)

    mcp = FastMCP(
        cfg.server_name,
        instructions=cfg.instructions or DEFAULT_INSTRUCTIONS,
    )

    diagnostics.register(mcp, runtime)
    services.register(mcp, runtime)
    nginx.register(mcp, runtime)

    @mcp.resource("linux://policy", mime_type="application/json")
    def policy_resource() -> str:
        """Machine-readable summary of the active policy."""
        data = {
            "policy_file": str(cfg.path),
            "write_enabled": cfg.allow_write,
            "allowed_units": sorted(cfg.allowed_units),
            "allowed_actions": sorted(cfg.allowed_actions),
            "nginx_parameters": {
                name: {
                    "context": spec.context,
                    "pattern": spec.pattern,
                    "description": spec.description,
                }
                for name, spec in sorted(cfg.nginx_parameters.items())
            },
            "helper_paths": {
                "nginx": str(cfg.helper("nginx")),
                "service": str(cfg.helper("service")),
            },
            "use_sudo": cfg.use_sudo,
            "audit_log": str(cfg.audit_log),
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    @mcp.prompt(title="Triage a systemd service")
    def triage_service(unit: str) -> str:
        """Build a read-only incident triage plan for a service."""
        return (
            f"Investigate the systemd unit '{unit}' on this Linux host.\n\n"
            "1. Call service_status to capture the current state.\n"
            "2. Call service_logs with a useful window (start with 50 lines).\n"
            "3. Call failed_units and disk_usage for host context.\n"
            "4. Summarise the likely cause, evidence, and the safest next action.\n"
            "Do not start, stop or restart anything unless the user explicitly asks "
            "and approves the action."
        )

    @mcp.prompt(title="Change an nginx parameter safely")
    def change_nginx_parameter(parameter: str, value: str) -> str:
        """Build a safe, reviewable nginx change plan."""
        return (
            f"Change the nginx parameter '{parameter}' to '{value}' safely.\n\n"
            "1. Call nginx_info and nginx_get_parameter to capture the current state.\n"
            "2. Call nginx_set_parameter with dry_run=true and show the diff.\n"
            "3. Explain the impact in one or two sentences.\n"
            "4. Only after the user approves, call nginx_set_parameter again with "
            "dry_run=false and reload=true.\n"
            "5. Confirm the new value with nginx_get_parameter. If anything fails, "
            "list backups and propose a rollback."
        )

    @mcp.prompt(title="Host health report")
    def host_health_report() -> str:
        """Ask for a concise, evidence-based host health report."""
        return (
            "Produce a concise health report for this host. Use system_overview, "
            "failed_units, disk_usage, process_top, network_overview and "
            "listening_ports. Rank findings by severity and include the evidence "
            "for each finding. Do not change anything."
        )

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="linux-agent-mcp",
        description="MCP server for controlled Linux administration.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="stdio for a local client; sse/streamable-http for a network client.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for HTTP transports.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for HTTP transports.")
    parser.add_argument("--policy", default=None, help="Path to policy.toml.")
    args = parser.parse_args(argv)

    cfg = load_config(args.policy)
    mcp = create_server(cfg)

    print(
        f"[linux-agent] policy={cfg.path} write={'ON' if cfg.allow_write else 'OFF'} "
        f"units={','.join(sorted(cfg.allowed_units)) or '-'} transport={args.transport}",
        file=sys.stderr,
        flush=True,
    )
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
