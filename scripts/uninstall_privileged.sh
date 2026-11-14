#!/usr/bin/env bash
# Remove the privileged helpers and sudoers rule. Logs and backups are kept
# unless --purge is supplied.
set -euo pipefail

INSTALL_DIR="/usr/local/libexec/linux-agent"
CONF_DIR="/etc/linux-agent"
SUDOERS_FILE="/etc/sudoers.d/linux-agent"
LOG_DIR="/var/log/linux-agent"
BACKUP_ROOT="/var/backups/linux-agent"
PURGE=0

[[ "${1:-}" == "--purge" ]] && PURGE=1
[[ "$EUID" -eq 0 ]] || {
    echo "ERROR: run as root (sudo)." >&2
    exit 1
}

rm -f -- "$SUDOERS_FILE"
rm -rf -- "$INSTALL_DIR"
rm -rf -- "$CONF_DIR"

if [[ "$PURGE" -eq 1 ]]; then
    rm -rf -- "$LOG_DIR" "$BACKUP_ROOT"
    echo "Removed helpers, configuration, logs and backups."
else
    echo "Removed helpers, configuration and sudoers rule."
    echo "Kept ${LOG_DIR} and ${BACKUP_ROOT}; use --purge to delete them."
fi
