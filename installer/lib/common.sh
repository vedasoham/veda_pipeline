#!/usr/bin/env bash
# VEDA installer :: shared helpers (logging, state, retries, guards).
# Sourced by install.sh, veda-doctor and uninstall.sh. Not executable on its own.

# ---------------------------------------------------------------------------
# Colour / symbols  (disabled when not a tty or when NO_COLOR is set)
# ---------------------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'; C_CYAN=$'\033[36m'
else
    C_RESET=''; C_BOLD=''; C_DIM=''
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_CYAN=''
fi

# ---------------------------------------------------------------------------
# Logging.  Everything also lands in $VEDA_LOG (set by install.sh).
# ---------------------------------------------------------------------------
veda::_stamp() { date '+%Y-%m-%d %H:%M:%S'; }

veda::_log_raw() {
    # $1 = plain text for the log file
    [[ -n "${VEDA_LOG:-}" ]] && printf '[%s] %s\n' "$(veda::_stamp)" "$1" >>"$VEDA_LOG"
    return 0
}

log()      { printf '   %s\n' "$*";                           veda::_log_raw "$*"; }
log_info() { printf '   %s%s%s\n' "$C_CYAN" "$*" "$C_RESET";  veda::_log_raw "INFO  $*"; }
log_ok()   { printf '   %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; veda::_log_raw "OK    $*"; }
log_warn() { printf '   %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; veda::_log_raw "WARN  $*"; }
log_err()  { printf '   %s✘%s %s\n' "$C_RED" "$C_RESET" "$*" >&2;  veda::_log_raw "ERROR $*"; }
log_skip() { printf '   %s·%s %s %s(already done)%s\n' "$C_DIM" "$C_RESET" "$*" "$C_DIM" "$C_RESET"; veda::_log_raw "SKIP  $*"; }

log_debug() {
    [[ "${VEDA_VERBOSE:-0}" == 1 ]] && printf '   %s» %s%s\n' "$C_DIM" "$*" "$C_RESET"
    veda::_log_raw "DEBUG $*"
}

banner() {
    printf '\n%s%s%s\n' "$C_BOLD$C_BLUE" "══════════════════════════════════════════════════════════════" "$C_RESET"
    printf '%s %s%s\n' "$C_BOLD$C_BLUE" "$*" "$C_RESET"
    printf '%s%s%s\n' "$C_BOLD$C_BLUE" "══════════════════════════════════════════════════════════════" "$C_RESET"
    veda::_log_raw "=== $* ==="
}

stage_header() {
    printf '\n%s▶ %s%s\n' "$C_BOLD" "$*" "$C_RESET"
    veda::_log_raw "--- stage: $* ---"
}

die() {
    log_err "$*"
    printf '\n%sInstallation aborted.%s Full log: %s\n' "$C_RED$C_BOLD" "$C_RESET" "${VEDA_LOG:-<none>}"
    exit 1
}

# ---------------------------------------------------------------------------
# Error trap.  install.sh installs this; it reports where things broke.
# ---------------------------------------------------------------------------
veda::on_error() {
    local code=$1 line=$2 src=$3 func=$4
    printf '\n%s╔══════════════════════════════════════════════════════════════╗%s\n' "$C_RED$C_BOLD" "$C_RESET"
    printf '%s║  VEDA install failed                                         ║%s\n' "$C_RED$C_BOLD" "$C_RESET"
    printf '%s╚══════════════════════════════════════════════════════════════╝%s\n' "$C_RED$C_BOLD" "$C_RESET"
    printf '   exit code : %s\n' "$code"
    printf '   location  : %s:%s  (in %s)\n' "${src##*/}" "$line" "$func"
    printf '   stage     : %s\n' "${VEDA_CURRENT_STAGE:-unknown}"
    printf '   log       : %s\n\n' "${VEDA_LOG:-<none>}"
    printf '   Completed stages are remembered. Re-run %s./install.sh%s to\n' "$C_BOLD" "$C_RESET"
    printf '   pick up from here, or %s./install.sh --from %s%s to retry this one.\n\n' \
        "$C_BOLD" "${VEDA_CURRENT_STAGE:-<stage>}" "$C_RESET"
    veda::_log_raw "FATAL exit=$code at ${src}:${line} in ${func} stage=${VEDA_CURRENT_STAGE:-unknown}"
    exit "$code"
}

# ---------------------------------------------------------------------------
# Command execution helpers
# ---------------------------------------------------------------------------

# run <description> <cmd...>   -- logs the command, streams output to the log,
#                                 shows it on screen only in --verbose mode.
run() {
    local desc=$1; shift
    local rc=0
    log_debug "exec: $*"
    veda::_log_raw "CMD   $*"

    if [[ "${VEDA_DRY_RUN:-0}" == 1 ]]; then
        log "${C_DIM}would run:${C_RESET} $*"
        return 0
    fi

    if [[ "${VEDA_VERBOSE:-0}" == 1 ]]; then
        "$@" 2>&1 | tee -a "${VEDA_LOG:-/dev/null}"
        rc=${PIPESTATUS[0]}
    else
        # Capture the status with `|| rc=$?`, never with `if ! cmd; then rc=$?`:
        # `!` inverts $? to 0, and a preceding `local rc=$?` would capture the
        # `local` builtin's own status instead. Both make every failure look
        # like a success.
        "$@" >>"${VEDA_LOG:-/dev/null}" 2>&1 || rc=$?
    fi

    if (( rc != 0 )); then
        log_err "$desc failed (exit $rc)"
        if [[ "${VEDA_VERBOSE:-0}" != 1 && -n "${VEDA_LOG:-}" ]]; then
            log_err "last 25 log lines:"
            tail -25 "$VEDA_LOG" | sed 's/^/        /' >&2
        fi
        return "$rc"
    fi
    return 0
}

# retry <attempts> <description> <cmd...>  -- exponential backoff for network work.
retry() {
    local attempts=$1 desc=$2; shift 2
    local n=1 delay=4
    while :; do
        if run "$desc (attempt $n/$attempts)" "$@"; then
            return 0
        fi
        if (( n >= attempts )); then
            log_err "$desc failed after $attempts attempts"
            return 1
        fi
        log_warn "$desc failed; retrying in ${delay}s"
        sleep "$delay"
        n=$(( n + 1 )); delay=$(( delay * 2 ))
    done
}

have() { command -v "$1" >/dev/null 2>&1; }

# Strip leading and trailing whitespace. Lives here rather than in tools.sh
# because mirror.sh is sourced earlier and parses the same pipe-delimited
# manifests -- a missing _trim there silently broke checksum lookup.
veda::_trim() {
    local s=$1
    s="${s#"${s%%[![:space:]]*}"}"
    printf '%s' "${s%"${s##*[![:space:]]}"}"
}

require_cmd() {
    have "$1" || die "required command '$1' not found. $2"
}

# ---------------------------------------------------------------------------
# Stage state  -- every stage records a marker so re-runs resume cleanly.
# ---------------------------------------------------------------------------
stage_done()      { [[ -f "$VEDA_STATE/$1.done" ]]; }
mark_stage_done() { mkdir -p "$VEDA_STATE"; date '+%Y-%m-%dT%H:%M:%S%z' >"$VEDA_STATE/$1.done"; }
clear_stage()     { rm -f "$VEDA_STATE/$1.done"; }

# should_run <stage> -- false when the stage is already complete and --force
# was not passed. Also honours --from / --only.
should_run() {
    local s=$1
    if [[ -n "${VEDA_ONLY:-}" ]]; then
        # A stage named with --only always runs, done-marker or not. This is
        # the marker bypass that --only needs, and it is deliberately NOT
        # VEDA_FORCE: running a stage again is cheap and idempotent, whereas
        # forcing it deletes and rebuilds its contents.
        [[ "$VEDA_ONLY" == "$s" ]] && return 0
        return 1
    fi
    if [[ "${VEDA_FORCE:-0}" == 1 ]]; then return 0; fi
    if stage_done "$s"; then return 1; fi
    return 0
}

# ---------------------------------------------------------------------------
# Interaction.  In --yes / non-tty mode every prompt takes its default.
# ---------------------------------------------------------------------------
confirm() {
    local prompt=$1 default=${2:-y} reply
    if [[ "${VEDA_ASSUME_YES:-0}" == 1 || ! -t 0 ]]; then
        log_debug "auto-answering '$prompt' -> $default"
        [[ "$default" == y ]]
        return
    fi
    local hint='[Y/n]'; [[ "$default" == n ]] && hint='[y/N]'
    read -r -p "   ${C_BOLD}?${C_RESET} $prompt $hint " reply
    reply=${reply:-$default}
    [[ ${reply,,} == y* ]]
}

pause_for() {
    local prompt=$1
    if [[ "${VEDA_ASSUME_YES:-0}" == 1 || ! -t 0 ]]; then
        log_warn "non-interactive: not waiting for '$prompt'"
        return 1
    fi
    read -r -p "   ${C_BOLD}↵${C_RESET} $prompt " _
    return 0
}

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

# human_gib <kib> -> "12.3 GiB"
human_gib() { awk -v k="$1" 'BEGIN{printf "%.1f GiB", k/1048576}'; }

# free_kib <path> -- available space on the filesystem holding <path>
free_kib() {
    local p=$1
    while [[ ! -d "$p" ]]; do p=$(dirname "$p"); done
    df -Pk "$p" | awk 'NR==2{print $4}'
}

# atomic_write <dest>  -- content on stdin; never leaves a half-written file.
atomic_write() {
    local dest=$1 tmp
    tmp=$(mktemp "${dest}.XXXXXX")
    cat >"$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$dest"
}

# backup_once <file>  -- keep a pristine .veda-orig copy before first patch.
backup_once() {
    local f=$1
    [[ -f "$f" ]] || return 0
    if [[ ! -f "$f.veda-orig" ]]; then
        cp -p "$f" "$f.veda-orig"
        log_debug "backed up $f -> $f.veda-orig"
    fi
}

# setenv_patch <file> <VAR> <value>  -- rewrite `setenv VAR ...` in a tcsh
# wrapper, preserving the original whitespace style.
setenv_patch() {
    local f=$1 var=$2 val=$3
    [[ -f "$f" ]] || { log_warn "cannot patch missing file $f"; return 1; }
    grep -qE "^[[:space:]]*setenv[[:space:]]+${var}[[:space:]]" "$f" || {
        log_warn "no 'setenv $var' line in ${f##*/}; leaving untouched"
        return 1
    }
    backup_once "$f"
    sed -i -E "s|^([[:space:]]*setenv[[:space:]]+${var}[[:space:]]+)[^[:space:]].*|\1${val}|" "$f"
    log_debug "patched ${f##*/}: $var -> $val"
}

# strip_mac_cruft <dir>  -- AppleDouble/._* files make several of these tools
# choke (they get globbed as input). Safe to run repeatedly.
strip_mac_cruft() {
    local d=$1
    [[ -d "$d" ]] || return 0
    find "$d" -name '._*' -type f -delete 2>/dev/null || true
    find "$d" -name '.DS_Store' -type f -delete 2>/dev/null || true
    find "$d" -name '__MACOSX' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    return 0
}

export -f have 2>/dev/null || true
