#!/usr/bin/env bash
# Install root-owned helpers and a narrowly scoped sudoers rule.
#
# Run once as root:
#   sudo scripts/install_privileged.sh --user "$USER" --units "nginx"
#
# The helpers themselves validate every argument. The invoking user must not be
# able to modify them, which is why they are copied to /usr/local/libexec and
# owned by root.
set -euo pipefail

INSTALL_DIR="/usr/local/libexec/linux-agent"
CONF_DIR="/etc/linux-agent"
SUDOERS_FILE="/etc/sudoers.d/linux-agent"
BACKUP_DIR="/var/backups/linux-agent/nginx"
LOG_DIR="/var/log/linux-agent"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-}"
UNITS="nginx"
TMP_SUDOERS=""

usage() {
    cat <<'EOF'
Usage: sudo scripts/install_privileged.sh [--user USER] [--units "nginx cron"]

  --user USER    user allowed to invoke the helpers (default: $SUDO_USER)
  --units LIST   space/comma-separated units allowed for service actions
                 (default: nginx)
EOF
}

cleanup() {
    [[ -n "$TMP_SUDOERS" && -f "$TMP_SUDOERS" ]] && rm -f "$TMP_SUDOERS"
}
trap cleanup EXIT

while (($# > 0)); do
    case "$1" in
        --user)
            TARGET_USER="${2:-}"
            shift 2
            ;;
        --units)
            UNITS="${2:-}"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage
            echo "ERROR: unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

[[ "$EUID" -eq 0 ]] || {
    echo "ERROR: run this script as root (sudo)." >&2
    exit 1
}
[[ -n "$TARGET_USER" ]] || {
    echo "ERROR: --user is required when SUDO_USER is not set." >&2
    exit 1
}
[[ "$TARGET_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
    echo "ERROR: invalid user name: $TARGET_USER" >&2
    exit 1
}
id "$TARGET_USER" >/dev/null 2>&1 || {
    echo "ERROR: user does not exist: $TARGET_USER" >&2
    exit 1
}
[[ -x "$REPO_DIR/scripts/nginx_param.sh" ]] || {
    echo "ERROR: scripts/nginx_param.sh not found or not executable." >&2
    exit 1
}
[[ -x "$REPO_DIR/scripts/service_ctl.sh" ]] || {
    echo "ERROR: scripts/service_ctl.sh not found or not executable." >&2
    exit 1
}
command -v visudo >/dev/null 2>&1 || {
    echo "ERROR: visudo is required (package: sudo)." >&2
    exit 1
}

echo "[1/5] Installing root-owned helpers in ${INSTALL_DIR}"
install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -o root -g root -m 0755 "$REPO_DIR/scripts/nginx_param.sh" "$INSTALL_DIR/nginx_param.sh"
install -o root -g root -m 0755 "$REPO_DIR/scripts/service_ctl.sh" "$INSTALL_DIR/service_ctl.sh"

echo "[2/5] Writing allow-list and directories"
install -d -o root -g root -m 0755 "$CONF_DIR"
: >"${CONF_DIR}/allowed-units"
for unit in ${UNITS//,/ }; do
    [[ -n "$unit" ]] || continue
    printf '%s\n' "${unit%.service}" >>"${CONF_DIR}/allowed-units"
done
chmod 0644 "${CONF_DIR}/allowed-units"
install -d -o root -g root -m 0700 /var/backups/linux-agent
install -d -o root -g root -m 0700 "$BACKUP_DIR"
if getent group adm >/dev/null 2>&1; then
    install -d -o root -g adm -m 0750 "$LOG_DIR"
else
    install -d -o root -g root -m 0750 "$LOG_DIR"
fi

echo "[3/5] Creating and validating the sudoers rule"
TMP_SUDOERS="$(mktemp)"
cat >"$TMP_SUDOERS" <<EOF
# Managed by linux-agent/scripts/install_privileged.sh. Do not edit by hand.
Cmnd_Alias LINUX_AGENT_HELPERS = ${INSTALL_DIR}/nginx_param.sh, ${INSTALL_DIR}/service_ctl.sh
${TARGET_USER} ALL=(root) NOPASSWD: LINUX_AGENT_HELPERS
EOF
if ! visudo -cf "$TMP_SUDOERS" >/dev/null; then
    echo "ERROR: generated sudoers rule is invalid; nothing was changed." >&2
    exit 1
fi
install -o root -g root -m 0440 "$TMP_SUDOERS" "$SUDOERS_FILE"

echo "[4/5] Verifying non-interactive sudo"
if sudo -n -u root -- "$INSTALL_DIR/nginx_param.sh" list >/dev/null 2>&1; then
    echo "  sudo -n: OK"
else
    echo "  WARNING: sudo -n could not run the helper as ${TARGET_USER}." >&2
    echo "  Check that ${TARGET_USER} is permitted to use sudo and that the rule is loaded." >&2
fi

echo "[5/5] Done"
cat <<EOF

Installed:
  helpers       ${INSTALL_DIR}
  allow-list    ${CONF_DIR}/allowed-units
  sudoers       ${SUDOERS_FILE}
  backups       ${BACKUP_DIR}
  audit logs    ${LOG_DIR}

Enable write tools for a session:
  LINUX_AGENT_ALLOW_WRITE=1 .venv/bin/python agent.py

If you use a different policy file, set:
  LINUX_AGENT_HELPER_DIR=${INSTALL_DIR}
EOF
