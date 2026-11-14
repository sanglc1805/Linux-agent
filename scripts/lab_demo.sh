#!/usr/bin/env bash
# Isolated end-to-end demo: real MCP server + real nginx helper, fake nginx and
# systemctl, temporary configuration tree. No root, no changes to /etc/nginx.
#
#   bash scripts/lab_demo.sh
#   LAB_AGENT=1 bash scripts/lab_demo.sh  # also run one natural-language agent turn
#   KEEP=1  bash scripts/lab_demo.sh    # keep the lab directory for inspection
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_TMP="${TMPDIR:-/tmp}"
[[ -d /tmp/opencode ]] && BASE_TMP=/tmp/opencode
LAB="$(mktemp -d "${BASE_TMP}/linux-agent-lab.XXXXXX")"

cleanup() {
    if [[ "${KEEP:-0}" == "1" ]]; then
        echo
        echo "Lab kept at: $LAB"
    else
        rm -rf -- "$LAB"
        echo
        echo "Lab removed (set KEEP=1 to keep artifacts)."
    fi
}
trap cleanup EXIT

mkdir -p "$LAB/etc/conf.d" "$LAB/backups" "$LAB/log"

cat >"$LAB/etc/nginx.conf" <<'EOF'
# Lab nginx.conf - not the real /etc/nginx/nginx.conf
user www-data;
worker_processes auto;
events {
    worker_connections 768;
}
http {
    include /etc/nginx/mime.types;
    include /etc/nginx/conf.d/*.conf;
}
EOF

cat >"$LAB/fake-nginx" <<'EOF'
#!/bin/sh
if [ "$1" = "-v" ]; then
    echo "nginx version: nginx/1.28.3 (lab)" >&2
    exit 0
fi
if [ "$1" = "-t" ]; then
    echo "nginx: configuration file test is successful (lab)" >&2
    exit 0
fi
exit 0
EOF
chmod +x "$LAB/fake-nginx"

cat >"$LAB/fake-systemctl" <<'EOF'
#!/bin/sh
if [ "$1" = "is-active" ]; then
    echo active
fi
exit 0
EOF
chmod +x "$LAB/fake-systemctl"

cat >"$LAB/nginx-helper" <<EOF
#!/bin/sh
export NGINX_CONF="$LAB/etc/nginx.conf"
export NGINX_CONF_DIR="$LAB/etc"
export NGINX_DROPIN_DIR="$LAB/etc/conf.d"
export NGINX_DROPIN="$LAB/etc/conf.d/99-linux-agent.conf"
export NGINX_BIN="$LAB/fake-nginx"
export NGINX_UNIT="nginx"
export SYSTEMCTL_BIN="$LAB/fake-systemctl"
export BACKUP_DIR="$LAB/backups"
export AUDIT_LOG="$LAB/log/nginx-audit.log"
export TMPDIR="$LAB"
exec "$ROOT/scripts/nginx_param.sh" "\$@"
EOF
chmod +x "$LAB/nginx-helper"

export LINUX_AGENT_ALLOW_WRITE=1
export LINUX_AGENT_USE_SUDO=0
export LINUX_AGENT_NGINX_HELPER="$LAB/nginx-helper"
export LINUX_AGENT_AUDIT_LOG="$LAB/log/server-audit.jsonl"
export LINUX_AGENT_POLICY="$ROOT/config/policy.toml"

echo "=== Linux Agent isolated lab ==="
echo "Lab directory: $LAB"
echo "Initial nginx.conf:"
sed 's/^/  /' "$LAB/etc/nginx.conf"
echo

.venv/bin/python - "$LAB" "$ROOT" <<'PY'
import asyncio
import json
import os
import re
import sys

from linux_agent.agent.client import open_stdio_session

LAB = sys.argv[1]
ROOT = sys.argv[2]


def text(result):
    return "\n".join(getattr(item, "text", "") for item in result.content)


async def main():
    stack, session = await open_stdio_session(
        sys.executable,
        [os.path.join(ROOT, "server.py")],
        cwd=ROOT,
        env=dict(os.environ),
    )
    async with stack:
        print("--- 1. Current managed parameters (read-only) ---")
        result = await session.call_tool("nginx_list_parameters", {})
        print(text(result))

        print("\n--- 2. Dry run: client_max_body_size 20m (no write) ---")
        result = await session.call_tool(
            "nginx_set_parameter",
            {"parameter": "client_max_body_size", "value": "20m", "dry_run": True},
        )
        print(text(result))

        print("\n--- 3. Apply: client_max_body_size 20m (validated + backed up) ---")
        result = await session.call_tool(
            "nginx_set_parameter",
            {
                "parameter": "client_max_body_size",
                "value": "20m",
                "dry_run": False,
                "reload": False,
            },
        )
        print(text(result))

        print("\n--- 4. Verify the new value ---")
        result = await session.call_tool(
            "nginx_get_parameter", {"parameter": "client_max_body_size"}
        )
        print(text(result))

        print("\n--- 5. Backups ---")
        result = await session.call_tool("nginx_list_backups", {})
        backups_text = text(result)
        print(backups_text)
        match = re.search(r"id:\s*(\S+)", backups_text)
        if not match:
            print("No backup id found; cannot demonstrate rollback.")
            return
        backup_id = match.group(1)

        print(f"\n--- 6. Roll back {backup_id} ---")
        result = await session.call_tool(
            "nginx_rollback",
            {"backup_id": backup_id, "dry_run": False, "reload": False},
        )
        print(text(result))

        print("\n--- 7. Verify the value after rollback ---")
        result = await session.call_tool(
            "nginx_get_parameter", {"parameter": "client_max_body_size"}
        )
        print(text(result))

        print("\n--- 8. Server audit tail ---")
        audit_path = os.environ["LINUX_AGENT_AUDIT_LOG"]
        with open(audit_path, encoding="utf-8") as handle:
            lines = handle.readlines()[-5:]
        for line in lines:
            entry = json.loads(line)
            print(
                f"  {entry['ts']} {entry['tool']:<22} ok={entry['ok']} "
                f"{entry.get('duration_ms', 0)}ms"
            )


asyncio.run(main())
PY

echo
echo "Final lab nginx.conf and managed drop-in:"
sed 's/^/  /' "$LAB/etc/nginx.conf"
if [[ -f "$LAB/etc/conf.d/99-linux-agent.conf" ]]; then
    sed 's/^/  /' "$LAB/etc/conf.d/99-linux-agent.conf"
else
    echo "  (managed drop-in absent, as expected after rollback)"
fi

if [[ "${LAB_AGENT:-0}" == "1" ]]; then
    echo
    echo "=== Natural-language agent turn (Groq) ==="
    .venv/bin/python "$ROOT/agent.py" \
        --prompt "Inspect the managed nginx parameters and show a dry-run diff for client_max_body_size=30m. Do not apply it." \
        --max-steps 5
fi
