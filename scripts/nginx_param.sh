#!/usr/bin/env bash
# Safely change a small, allow-listed set of nginx parameters.
#
# Installed root-owned in /usr/local/libexec/linux-agent and normally invoked
# through sudo. Every change is validated, backed up, tested with `nginx -t`,
# and automatically rolled back if the test or reload fails.
#
# Environment overrides exist for testing (NGINX_CONF, NGINX_DROPIN_DIR,
# NGINX_BIN, BACKUP_DIR, AUDIT_LOG, SYSTEMCTL_BIN) and must not be set in the
# installed sudoers context.
set -euo pipefail
IFS=$'\n\t'

NGINX_CONF="${NGINX_CONF:-/etc/nginx/nginx.conf}"
NGINX_CONF_DIR="${NGINX_CONF_DIR:-/etc/nginx}"
NGINX_DROPIN_DIR="${NGINX_DROPIN_DIR:-/etc/nginx/conf.d}"
NGINX_DROPIN="${NGINX_DROPIN:-${NGINX_DROPIN_DIR}/99-linux-agent.conf}"
NGINX_BIN="${NGINX_BIN:-nginx}"
NGINX_UNIT="${NGINX_UNIT:-nginx}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/linux-agent/nginx}"
AUDIT_LOG="${AUDIT_LOG:-/var/log/linux-agent/nginx-audit.log}"
LOCK_FILE="${LOCK_FILE:-${BACKUP_DIR}/.lock}"

declare -A PARAM_CONTEXT=(
    [worker_processes]=main
    [worker_connections]=events
    [client_max_body_size]=http
    [keepalive_timeout]=http
    [client_body_timeout]=http
    [send_timeout]=http
    [gzip]=http
    [server_tokens]=http
)

declare -A PARAM_DESC=(
    [worker_processes]="auto or 1-64"
    [worker_connections]="128-65535"
    [client_max_body_size]="0 or size (k/m/g), max 10g"
    [keepalive_timeout]="0-600 seconds"
    [client_body_timeout]="0-300 seconds"
    [send_timeout]="0-300 seconds"
    [gzip]="on or off"
    [server_tokens]="on or off"
)

HTTP_ORDER=(
    client_max_body_size
    keepalive_timeout
    client_body_timeout
    send_timeout
    gzip
    server_tokens
)

usage() {
    cat <<'EOF' >&2
Usage:
  nginx_param.sh list
  nginx_param.sh get <parameter>
  nginx_param.sh set <parameter> <value> [--dry-run] [--reload]
  nginx_param.sh test
  nginx_param.sh status
  nginx_param.sh list-backups
  nginx_param.sh diff-backup <backup-id>
  nginx_param.sh rollback <backup-id> [--dry-run] [--reload]

Managed parameters:
  worker_processes       (main)   auto or 1-64
  worker_connections     (events) 128-65535
  client_max_body_size   (http)   0 or size such as 20m (max 10g)
  keepalive_timeout      (http)   0-600 seconds
  client_body_timeout    (http)   0-300 seconds
  send_timeout           (http)   0-300 seconds
  gzip                   (http)   on/off
  server_tokens          (http)   on/off
EOF
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

audit() {
    local message="$1"
    local line
    line="$(now) ${message}"
    logger -t linux-agent-nginx -- "$line" 2>/dev/null || true
    if mkdir -p "$(dirname "$AUDIT_LOG")" 2>/dev/null; then
        printf '%s\n' "$line" >>"$AUDIT_LOG" 2>/dev/null || true
    fi
}

fail() {
    echo "ERROR: $*" >&2
    audit "result=error error=$*"
    exit 1
}

ok() {
    echo "$*"
    audit "result=ok $*"
}

require_param() {
    local name="$1"
    [[ -n "${PARAM_CONTEXT[$name]:-}" ]] || {
        local allowed
        allowed="$(printf '%s\n' "${!PARAM_CONTEXT[@]}" | sort | tr '\n' ' ')"
        fail "unknown parameter '$name'. Allowed: $allowed"
    }
}

validate_value() {
    local name="$1" value="$2"

    case "$name" in
        worker_processes)
            if [[ "$value" != "auto" ]]; then
                if ! [[ "$value" =~ ^[1-9][0-9]?$ ]] || ((value < 1 || value > 64)); then
                    fail "worker_processes must be auto or 1-64"
                fi
            fi
            ;;
        worker_connections)
            if ! [[ "$value" =~ ^[1-9][0-9]{0,5}$ ]] || ((value < 128 || value > 65535)); then
                fail "worker_connections must be 128-65535"
            fi
            ;;
        client_max_body_size)
            if [[ "$value" == "0" ]]; then
                return 0
            fi
            if ! [[ "$value" =~ ^([1-9][0-9]{0,4})([kKmMgG]?)$ ]]; then
                fail "client_max_body_size must be 0 or a size such as 20m (max 10g)"
            fi
            local number="${BASH_REMATCH[1]}" unit="${BASH_REMATCH[2],,}" multiplier=1
            case "$unit" in
                k) multiplier=1024 ;;
                m) multiplier=$((1024 * 1024)) ;;
                g) multiplier=$((1024 * 1024 * 1024)) ;;
                "") multiplier=1 ;;
            esac
            if ((number * multiplier > 10 * 1024 * 1024 * 1024)); then
                fail "client_max_body_size may not exceed 10g"
            fi
            ;;
        keepalive_timeout)
            if ! [[ "$value" =~ ^[0-9]{1,4}$ ]] || ((value > 600)); then
                fail "keepalive_timeout must be 0-600 seconds"
            fi
            ;;
        client_body_timeout | send_timeout)
            if ! [[ "$value" =~ ^[0-9]{1,3}$ ]] || ((value > 300)); then
                fail "$name must be 0-300 seconds"
            fi
            ;;
        gzip | server_tokens)
            [[ "$value" == "on" || "$value" == "off" ]] || fail "$name must be on or off"
            ;;
        *)
            fail "no validator for $name"
            ;;
    esac
}

current_value() {
    local name="$1" value=""
    case "${PARAM_CONTEXT[$name]}" in
        http)
            if [[ -f "$NGINX_DROPIN" ]]; then
                value="$(grep -hE "^[[:space:]]*${name}[[:space:]]+[^;]+;" "$NGINX_DROPIN" 2>/dev/null |
                    tail -n 1 | sed -E "s/^[[:space:]]*${name}[[:space:]]+([^;]+);.*/\1/" || true)"
            fi
            if [[ -z "$value" ]]; then
                value="$(grep -RhE "^[[:space:]]*${name}[[:space:]]+[^;]+;" "$NGINX_CONF_DIR" \
                    --include='*.conf' 2>/dev/null |
                    grep -v "^${NGINX_DROPIN}" |
                    tail -n 1 | sed -E "s/^[[:space:]]*${name}[[:space:]]+([^;]+);.*/\1/" || true)"
            fi
            ;;
        main | events)
            value="$(grep -hE "^[[:space:]]*${name}[[:space:]]+[^;]+;" "$NGINX_CONF" 2>/dev/null |
                tail -n 1 | sed -E "s/^[[:space:]]*${name}[[:space:]]+([^;]+);.*/\1/" || true)"
            ;;
    esac
    printf '%s' "$value"
}

new_backup_id() {
    printf '%s-%s' "$(date -u +%Y%m%dT%H%M%SZ)" "$$"
}

create_backup() {
    local action="$1" parameter="$2" old="$3" new="$4" id
    id="$(new_backup_id)"
    mkdir -p "${BACKUP_DIR}/${id}"
    chmod 700 "${BACKUP_DIR}" "${BACKUP_DIR}/${id}" 2>/dev/null || true

    : >"${BACKUP_DIR}/${id}/files.map"
    : >"${BACKUP_DIR}/${id}/absent.map"
    if [[ -f "$NGINX_CONF" ]]; then
        cp -a -- "$NGINX_CONF" "${BACKUP_DIR}/${id}/nginx.conf"
        printf '%s\t%s\n' "$NGINX_CONF" "nginx.conf" >>"${BACKUP_DIR}/${id}/files.map"
    else
        printf '%s\n' "$NGINX_CONF" >>"${BACKUP_DIR}/${id}/absent.map"
    fi
    if [[ -f "$NGINX_DROPIN" ]]; then
        cp -a -- "$NGINX_DROPIN" "${BACKUP_DIR}/${id}/dropin.conf"
        printf '%s\t%s\n' "$NGINX_DROPIN" "dropin.conf" >>"${BACKUP_DIR}/${id}/files.map"
    else
        printf '%s\n' "$NGINX_DROPIN" >>"${BACKUP_DIR}/${id}/absent.map"
    fi
    cat >"${BACKUP_DIR}/${id}/meta" <<EOF
action=${action}
parameter=${parameter}
old=${old}
new=${new}
created=$(now)
host=$(hostname)
EOF
    printf '%s' "$id"
}

restore_backup() {
    local id="$1"
    local dir="${BACKUP_DIR}/${id}"
    [[ -d "$dir" ]] || fail "backup not found: $id"
    [[ -f "${dir}/files.map" ]] || fail "backup is incomplete: $id"

    local original saved tmp
    while IFS=$'\t' read -r original saved; do
        [[ -n "$original" && -n "$saved" ]] || continue
        [[ -f "${dir}/${saved}" ]] || fail "missing backup file: ${dir}/${saved}"
        mkdir -p "$(dirname "$original")"
        tmp="$(mktemp "$(dirname "$original")/.linux-agent-restore.XXXXXX")"
        cp -- "${dir}/${saved}" "$tmp"
        if [[ -f "$original" ]]; then
            chmod --reference="$original" "$tmp" 2>/dev/null || chmod 0644 "$tmp"
            chown --reference="$original" "$tmp" 2>/dev/null || true
        else
            chmod 0644 "$tmp"
        fi
        mv -f -- "$tmp" "$original"
    done <"${dir}/files.map"

    # Files that did not exist before the backup must be removed on rollback.
    if [[ -f "${dir}/absent.map" ]]; then
        while IFS= read -r original; do
            [[ -n "$original" ]] && rm -f -- "$original"
        done <"${dir}/absent.map"
    fi
}

atomic_replace() {
    local source="$1" destination="$2" tmp
    mkdir -p "$(dirname "$destination")"
    tmp="$(mktemp "$(dirname "$destination")/.linux-agent-write.XXXXXX")"
    cp -- "$source" "$tmp"
    if [[ -f "$destination" ]]; then
        chmod --reference="$destination" "$tmp" 2>/dev/null || chmod 0644 "$tmp"
        chown --reference="$destination" "$tmp" 2>/dev/null || true
    else
        chmod 0644 "$tmp"
    fi
    mv -f -- "$tmp" "$destination"
}

render_http_candidate() {
    local parameter="$1" value="$2" output="$3"
    local -A values=()
    local line key val p

    if [[ -f "$NGINX_DROPIN" ]]; then
        while IFS= read -r line; do
            if [[ "$line" =~ ^[[:space:]]*([a-z_]+)[[:space:]]+([^;]+)\; ]]; then
                key="${BASH_REMATCH[1]}"
                val="${BASH_REMATCH[2]}"
                [[ -n "${PARAM_CONTEXT[$key]:-}" ]] && values["$key"]="$val"
            fi
        done <"$NGINX_DROPIN"
    fi
    values["$parameter"]="$value"

    {
        echo "# Managed by linux-agent - manual edits will be overwritten."
        echo "# Backups are stored in ${BACKUP_DIR}."
        for p in "${HTTP_ORDER[@]}"; do
            if [[ -n "${values[$p]:-}" ]]; then
                printf '%s %s;\n' "$p" "${values[$p]}"
            fi
        done
    } >"$output"
}

render_main_candidate() {
    local parameter="$1" value="$2" output="$3"
    local awk_status=0
    awk -v p="$parameter" -v v="$value" '
        BEGIN { done = 0 }
        {
            if (!done && $0 ~ ("^[[:space:]]*" p "[[:space:]]")) {
                match($0, /^[[:space:]]*/)
                indent = substr($0, RSTART, RLENGTH)
                print indent p " " v ";"
                done = 1
                next
            }
            print
        }
        END { if (!done) exit 3 }
    ' "$NGINX_CONF" >"$output" || awk_status=$?
    if [[ "$awk_status" -eq 3 ]]; then
        fail "directive '$parameter' was not found in ${NGINX_CONF}; refusing to guess its context"
    elif [[ "$awk_status" -ne 0 ]]; then
        fail "failed to render candidate configuration (awk exit ${awk_status})"
    fi
}

test_config() {
    local output
    if ! output="$("$NGINX_BIN" -t -c "$NGINX_CONF" 2>&1)"; then
        printf '%s\n' "$output" >&2
        return 1
    fi
    printf '%s\n' "$output"
}

reload_nginx() {
    "$SYSTEMCTL_BIN" reload "$NGINX_UNIT"
}

with_lock() {
    if command -v flock >/dev/null 2>&1; then
        mkdir -p "$BACKUP_DIR"
        exec 9>"$LOCK_FILE"
        flock -w 15 9 || fail "another configuration change is in progress"
    fi
}

show_diff() {
    local original="$1" candidate="$2" label="$3"
    if [[ -f "$original" ]]; then
        diff -u --label "${label} (current)" --label "${label} (proposed)" "$original" "$candidate" || true
    else
        diff -u --label "${label} (current: absent)" --label "${label} (proposed)" /dev/null "$candidate" || true
    fi
}

cmd_list() {
    local p current
    printf '%-24s %-7s %-5s %s\n' "PARAMETER" "CONTEXT" "VALUE" "ALLOWED"
    for p in worker_processes worker_connections "${HTTP_ORDER[@]}"; do
        current="$(current_value "$p")"
        printf '%-24s %-7s %-5s %s\n' "$p" "${PARAM_CONTEXT[$p]}" "${current:-unset}" "${PARAM_DESC[$p]}"
    done
}

cmd_get() {
    local parameter="$1" value
    require_param "$parameter"
    value="$(current_value "$parameter")"
    printf '%s\n' "${value:-unset}"
}

cmd_set() {
    local parameter="${1:-}" value="${2:-}"
    shift $(( $# >= 2 ? 2 : $# )) || true
    local dry_run=0 reload=0
    require_param "$parameter"
    validate_value "$parameter" "$value"

    while (($# > 0)); do
        case "$1" in
            --dry-run) dry_run=1 ;;
            --reload) reload=1 ;;
            *) fail "unknown option: $1" ;;
        esac
        shift
    done

    local context="${PARAM_CONTEXT[$parameter]}"
    local old candidate target
    old="$(current_value "$parameter")"
    [[ -n "$old" ]] || old="(unset)"
    candidate="$(mktemp "${TMPDIR:-/tmp}/linux-agent-nginx.XXXXXX")"
    # Expand the path now: the local variable is gone by the time EXIT runs.
    trap "rm -f '${candidate}'" EXIT

    if [[ "$context" == "http" ]]; then
        target="$NGINX_DROPIN"
        render_http_candidate "$parameter" "$value" "$candidate"
    else
        target="$NGINX_CONF"
        render_main_candidate "$parameter" "$value" "$candidate"
    fi

    if [[ -f "$target" ]] && cmp -s -- "$candidate" "$target"; then
        ok "no change: ${parameter} is already ${value}"
        return 0
    fi

    if [[ "$dry_run" -eq 1 ]]; then
        echo "DRY RUN: ${parameter}: ${old} -> ${value} (${context})"
        show_diff "$target" "$candidate" "${target}"
        audit "result=dry-run parameter=${parameter} old=${old} new=${value}"
        return 0
    fi

    with_lock
    local backup_id
    backup_id="$(create_backup set "$parameter" "$old" "$value")"
    atomic_replace "$candidate" "$target"

    local test_output
    if ! test_output="$(test_config)"; then
        restore_backup "$backup_id"
        audit "result=rolled-back parameter=${parameter} reason=config-test backup=${backup_id}"
        fail "nginx -t failed; configuration restored from backup ${backup_id}"
    fi

    if [[ "$reload" -eq 1 ]]; then
        if ! reload_nginx; then
            restore_backup "$backup_id"
            test_config >/dev/null 2>&1 || true
            reload_nginx >/dev/null 2>&1 || true
            audit "result=rolled-back parameter=${parameter} reason=reload backup=${backup_id}"
            fail "reload failed; configuration restored from backup ${backup_id}"
        fi
    fi

    local backup_name="nginx.conf"
    [[ "$context" == "http" ]] && backup_name="dropin.conf"
    local backup_file="${BACKUP_DIR}/${backup_id}/${backup_name}"
    local diff_text=""
    if [[ -f "$backup_file" ]]; then
        diff_text="$(diff -u --label "${target} (before)" --label "${target} (after)" \
            "$backup_file" "$target" || true)"
    fi
    ok "applied ${parameter} ${old} -> ${value} (${context}) backup=${backup_id} reload=$([[ $reload -eq 1 ]] && echo yes || echo no)"
    echo "Config test: passed"
    printf '%s\n' "$test_output" | sed 's/^/  /'
    echo "Backup: ${BACKUP_DIR}/${backup_id}"
    [[ -n "$diff_text" ]] && printf '%s\n' "$diff_text"
    return 0
}

cmd_test() {
    local output
    output="$(test_config)" || fail "nginx -t failed"
    echo "nginx -t: OK"
    printf '%s\n' "$output"
}

cmd_status() {
    echo "nginx: $("$NGINX_BIN" -v 2>&1 || true)"
    echo "unit: ${NGINX_UNIT} ($("$SYSTEMCTL_BIN" is-active "$NGINX_UNIT" 2>/dev/null || echo unknown))"
    echo
    cmd_list
    echo
    cmd_test
}

cmd_list_backups() {
    [[ -d "$BACKUP_DIR" ]] || {
        echo "(no backups)"
        return 0
    }
    local found=0 dir
    while IFS= read -r dir; do
        [[ -d "$dir" ]] || continue
        found=1
        printf '%s\n' "id: $(basename "$dir")"
        if [[ -f "${dir}/meta" ]]; then
            sed 's/^/  /' "${dir}/meta"
        fi
    done < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
        sort -rn | cut -d' ' -f2-)
    [[ "$found" -eq 1 ]] || echo "(no backups)"
}

cmd_diff_backup() {
    local id="$1" dir="${BACKUP_DIR}/${id}"
    local original saved
    [[ -d "$dir" ]] || fail "backup not found: $id"
    [[ -f "${dir}/files.map" ]] || fail "backup is incomplete: $id"
    while IFS=$'\t' read -r original saved; do
        [[ -n "$original" && -f "${dir}/${saved}" ]] || continue
        echo "=== ${original} ==="
        if [[ -f "$original" ]]; then
            diff -u --label "${original} (backup ${id})" --label "${original} (current)" \
                "${dir}/${saved}" "$original" || true
        else
            echo "(current file is absent)"
        fi
    done <"${dir}/files.map"
}

cmd_rollback() {
    local id="${1:-}" dry_run=0 reload=0
    [[ -n "$id" ]] || fail "backup id is required"
    [[ "$id" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || fail "invalid backup id: $id"
    shift || true
    while (($# > 0)); do
        case "$1" in
            --dry-run) dry_run=1 ;;
            --reload) reload=1 ;;
            *) fail "unknown option: $1" ;;
        esac
        shift
    done
    [[ -d "${BACKUP_DIR}/${id}" ]] || fail "backup not found: $id"

    if [[ "$dry_run" -eq 1 ]]; then
        echo "DRY RUN: would restore backup ${id}"
        cmd_diff_backup "$id"
        audit "result=dry-run-rollback backup=${id}"
        return 0
    fi

    with_lock
    local safety
    safety="$(create_backup rollback-safety "$id" "current" "restore")"
    restore_backup "$id"

    local test_output
    if ! test_output="$(test_config)"; then
        restore_backup "$safety"
        audit "result=rolled-back backup=${id} reason=config-test safety=${safety}"
        fail "restored configuration failed nginx -t; current state re-applied (safety ${safety})"
    fi
    if [[ "$reload" -eq 1 ]] && ! reload_nginx; then
        restore_backup "$safety"
        test_config >/dev/null 2>&1 || true
        reload_nginx >/dev/null 2>&1 || true
        audit "result=rolled-back backup=${id} reason=reload safety=${safety}"
        fail "reload failed; previous state re-applied (safety ${safety})"
    fi

    ok "rolled back to ${id} (safety backup=${safety} reload=$([[ $reload -eq 1 ]] && echo yes || echo no))"
    echo "Config test: passed"
    printf '%s\n' "$test_output" | sed 's/^/  /'
}

main() {
    local command="${1:-help}"
    [[ $# -gt 0 ]] && shift || true
    case "$command" in
        list) cmd_list ;;
        get) [[ $# -ge 1 ]] || fail "get requires a parameter"; cmd_get "$1" ;;
        set) [[ $# -ge 2 ]] || fail "set requires a parameter and value"; cmd_set "$@" ;;
        test) cmd_test ;;
        status) cmd_status ;;
        list-backups) cmd_list_backups ;;
        diff-backup) [[ $# -ge 1 ]] || fail "diff-backup requires an id"; cmd_diff_backup "$1" ;;
        rollback) cmd_rollback "$@" ;;
        -h | --help | help) usage ;;
        *) usage; fail "unknown command: $command" ;;
    esac
}

main "$@"
