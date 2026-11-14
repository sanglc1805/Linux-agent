#!/usr/bin/env bash
# Privileged systemd helper for Linux Agent.
#
# Installed root-owned in /usr/local/libexec/linux-agent and invoked through a
# narrowly scoped sudoers rule. It independently validates the action, unit
# name, and allow-list; it must not trust the caller.
set -euo pipefail
IFS=$'\n\t'

ACTION="${1:-}"
UNIT="${2:-}"
ALLOWED_FILE="${ALLOWED_UNITS_FILE:-/etc/linux-agent/allowed-units}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
ACTION_LOG="${ACTION_LOG:-/var/log/linux-agent/actions.log}"

usage() {
    echo "Usage: $0 <start|stop|restart|reload> <unit>" >&2
    echo "Allowed units are read from: $ALLOWED_FILE" >&2
}

audit() {
    local message="$1"
    local line
    line="$(date -u +%Y-%m-%dT%H:%M:%SZ) action=${ACTION} unit=${UNIT} ${message}"
    logger -t linux-agent-service -- "$line" 2>/dev/null || true
    if mkdir -p "$(dirname "$ACTION_LOG")" 2>/dev/null; then
        printf '%s\n' "$line" >>"$ACTION_LOG" 2>/dev/null || true
    fi
}

fail() {
    echo "ERROR: $*" >&2
    audit "result=error error=$*"
    exit 1
}

case "$ACTION" in
    start | stop | restart | reload) ;;
    -h | --help | help)
        usage
        exit 0
        ;;
    *)
        usage
        fail "invalid action: ${ACTION:-<empty>}"
        ;;
esac

[[ -n "$UNIT" ]] || fail "a unit name is required"

# Conservative systemd unit syntax. Reject option injection and path tricks.
if ! [[ "$UNIT" =~ ^[A-Za-z0-9][A-Za-z0-9@._:-]{0,127}$ ]] || [[ "$UNIT" == *..* ]]; then
    fail "invalid unit name: $UNIT"
fi

[[ -r "$ALLOWED_FILE" ]] || fail "allow-list not readable: $ALLOWED_FILE"

BASE="${UNIT%.service}"
ALLOWED=0
while IFS= read -r candidate; do
    [[ -z "$candidate" || "$candidate" == \#* ]] && continue
    candidate="${candidate%%#*}"
    candidate="$(printf '%s' "$candidate" | tr -d '[:space:]')"
    [[ -z "$candidate" ]] && continue
    candidate="${candidate%.service}"
    if [[ "$candidate" == "$BASE" ]]; then
        ALLOWED=1
        break
    fi
done <"$ALLOWED_FILE"

[[ "$ALLOWED" -eq 1 ]] || fail "unit '$UNIT' is not in the allow-list ($ALLOWED_FILE)"

TARGET="${BASE}.service"
audit "result=attempt"
if ! "$SYSTEMCTL_BIN" "$ACTION" -- "$TARGET"; then
    fail "systemctl $ACTION $TARGET failed"
fi

# Give systemd a moment to settle, then report the new state. A stopped unit is
# expected to be inactive; the action itself already succeeded.
sleep 0.5
STATE="$("$SYSTEMCTL_BIN" is-active "$TARGET" 2>/dev/null || true)"
audit "result=ok state=${STATE:-unknown}"
echo "OK: ${ACTION} ${TARGET} (state: ${STATE:-unknown})"
