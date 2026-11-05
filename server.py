import argparse
import re
import subprocess
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("linux-agent")

UNIT_RE = re.compile(r"[A-Za-z0-9@._-]+")  # chỉ cho phép tên unit hợp lệ

def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return (r.stdout + r.stderr).strip()

def check_unit(name: str) -> str:
    if not UNIT_RE.fullmatch(name) or name.startswith("-"):
        raise ValueError(f"Invalid unit name: {name!r}")
    return name

@mcp.tool()
def service_status(name: str) -> str:
    """Get the status of a systemd service (e.g. nginx, ssh)."""
    return run(["systemctl", "status", check_unit(name), "--no-pager", "-n", "10"])

@mcp.tool()
def tail_journal(unit: str, lines: int = 30) -> str:
    """Show the last N journal lines of a systemd unit."""
    lines = max(1, min(lines, 200))
    return run(["journalctl", "-u", check_unit(unit), "-n", str(lines), "--no-pager"])

@mcp.tool()
def disk_usage() -> str:
    """Show disk usage of all mounted filesystems."""
    return run(["df", "-h"])

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport", choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="stdio: chạy local, client tự spawn process. "
             "sse/streamable-http: mở cổng mạng cho client từ máy khác gọi vào."
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)
