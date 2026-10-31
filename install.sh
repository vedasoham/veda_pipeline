#!/usr/bin/env bash
#
#  VEDA -- one-shot installer
#  ===========================
#
#  Installs the whole pipeline on a fresh Ubuntu/Debian machine:
#  system packages, a private micromamba, four pinned Python environments,
#  every prediction tool it can legally download, the Docker images, and a
#  `veda` launcher. Then it runs each tool to prove the install works.
#
#  Usage:   ./install.sh [options]
#
#  Safe to re-run. Completed stages are remembered under ~/.veda/state and
#  skipped, so an interrupted or failed install resumes where it stopped.
#
#  Nothing outside these locations is modified:
#     ~/.veda/                      interpreters, envs, caches, logs, state
#     ~/.local/bin/veda             launcher symlink
#     ~/.local/share/applications/  desktop entry
#     <project>/resources/tools/    the tools themselves
#     <project>/veda_paths.json     generated machine-local paths
#     ~/.bashrc                     one guarded PATH line, only if you agree
#  Patched tool files are backed up next to the original as *.veda-orig.
#
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Locate ourselves (resolving symlinks) so the installer can be called from
# anywhere, including through a symlink on PATH.
# ---------------------------------------------------------------------------
VEDA_SELF=${BASH_SOURCE[0]}
while [[ -L "$VEDA_SELF" ]]; do
    _link=$(readlink "$VEDA_SELF")
    [[ "$_link" == /* ]] && VEDA_SELF=$_link || VEDA_SELF=$(dirname "$VEDA_SELF")/$_link
done
VEDA_APP_DIR=$(cd "$(dirname "$VEDA_SELF")" && pwd -P)
VEDA_INSTALLER_DIR="$VEDA_APP_DIR/installer"
VEDA_TOOLS_DIR="$VEDA_APP_DIR/resources/tools"

# ---------------------------------------------------------------------------
# Where VEDA keeps its own state. Override with VEDA_HOME=... ./install.sh
# ---------------------------------------------------------------------------
VEDA_HOME=${VEDA_HOME:-$HOME/.veda}
VEDA_MAMBA_ROOT="$VEDA_HOME/mamba"
VEDA_MAMBA_BIN="$VEDA_HOME/bin/micromamba"
VEDA_STATE="$VEDA_HOME/state"
VEDA_STAGING="$VEDA_HOME/staging"
VEDA_LOG_DIR="$VEDA_HOME/logs"

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
VEDA_VERBOSE=0
VEDA_FORCE=0
VEDA_ASSUME_YES=0
VEDA_DRY_RUN=0
VEDA_SKIP_DOCKER=0
VEDA_SKIP_SYSTEM=0
VEDA_SKIP_VERIFY=0
VEDA_CPU_ONLY=0
VEDA_OFFLINE=0
VEDA_PREWARM=0
VEDA_FROM=''
VEDA_ONLY=''
VEDA_ONLY_TOOL=''
VEDA_MIRROR=${VEDA_MIRROR:-}
VEDA_NO_MIRROR=0
VEDA_CURRENT_STAGE=''
VEDA_HAVE_SUDO=0
VEDA_DOCKER_OK=0
VEDA_CMD=veda

VEDA_STAGES=(preflight probe system mamba envs tools docker configure launcher verify)

usage() {
    cat <<'USAGE'
VEDA installer

  ./install.sh [options]

Options
  -y, --yes             take the default answer for every prompt
  -v, --verbose         stream all command output to the terminal
  -f, --force           redo stages that are already marked complete
      --from STAGE      start at STAGE, re-running it and everything after
      --only STAGE      run just STAGE, reusing whatever is already in place.
                        Add --force to rebuild its contents instead.
      --only-tool ID    with --only tools: handle a single tool (e.g. algpred2)
      --mirror URL      fetch the tools from a pre-packed mirror instead of
                        cloning them. Overrides installer/mirror.conf.
                          gh://owner/repo@tag      GitHub Release (recommended)
                          https://host/path/       any web server, S3, R2, B2
                          gdrive://<folder-id>     Google Drive (via gdown)
                          rclone://remote:path     any rclone remote
      --no-mirror       ignore any configured mirror; use upstream sources
      --skip-docker     do not install Docker or pull images
      --skip-system     do not touch apt (assumes tcsh/perl/git already present)
      --skip-verify     do not run the post-install checks
      --cpu-only        install CPU PyTorch even if an NVIDIA GPU is present
      --offline         no downloads; use only what is already on disk
      --prewarm         also pre-download DeepLoc's ESM weights (several GB)
      --dry-run         print what would happen, change nothing
  -h, --help            this text

Stages
  preflight  system, disk, memory and network checks
  system     apt packages (tcsh, perl, git-lfs, build tools)
  mamba      private micromamba under ~/.veda
  envs       the four pinned Python environments + PyTorch + DeepLoc2
  tools      download / stage / repair the prediction tools
  docker     Docker Engine and the Vaxign-ML and DeepTMHMM images
  configure  write veda_paths.json, patch the tools' hardcoded paths
  launcher   the `veda` command and desktop entry
  verify     run every tool and report what works

Examples
  ./install.sh                       full install, prompts where it matters
  ./install.sh -y                    unattended
  ./install.sh --from envs --force   rebuild the Python environments
  ./install.sh --only verify         just re-check an existing install
  ./install.sh --only tools --only-tool algpred2 --force
USAGE
}

while (( $# )); do
    case $1 in
        -y|--yes)       VEDA_ASSUME_YES=1 ;;
        -v|--verbose)   VEDA_VERBOSE=1 ;;
        -f|--force)     VEDA_FORCE=1 ;;
        --from)         VEDA_FROM=${2:?--from needs a stage name}; shift ;;
        --only)         VEDA_ONLY=${2:?--only needs a stage name}; shift ;;
        --only-tool)    VEDA_ONLY_TOOL=${2:?--only-tool needs a tool id}; shift ;;
        --mirror)       VEDA_MIRROR=${2:?--mirror needs a URL}; shift ;;
        --no-mirror)    VEDA_NO_MIRROR=1 ;;
        --skip-docker)  VEDA_SKIP_DOCKER=1 ;;
        --skip-system)  VEDA_SKIP_SYSTEM=1 ;;
        --skip-verify)  VEDA_SKIP_VERIFY=1 ;;
        --cpu-only)     VEDA_CPU_ONLY=1 ;;
        --offline)      VEDA_OFFLINE=1 ;;
        --prewarm)      VEDA_PREWARM=1 ;;
        --dry-run)      VEDA_DRY_RUN=1 ;;
        -h|--help)      usage; exit 0 ;;
        *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# NOTE: --only deliberately does NOT imply --force.
#
# It used to. The reasoning was that --only <stage> must bypass the "already
# done" marker or the stage would skip itself -- which is true, but VEDA_FORCE
# means something much bigger: "destroy and rebuild this stage's contents".
# Conflating them made `./install.sh --only envs` delete and rebuild all four
# environments, a ~25 minute, multi-GB operation, when the user had asked for
# the cheapest possible targeted re-run.
#
# should_run() now treats an explicitly requested stage as runnable on its own,
# so the marker bypass no longer needs VEDA_FORCE. Rebuilding is opt-in:
#     ./install.sh --only envs            reuse what exists, fix what is wrong
#     ./install.sh --only envs --force    delete and rebuild

validate_stage() {
    local s=$1 k
    for k in "${VEDA_STAGES[@]}"; do [[ "$k" == "$s" ]] && return 0; done
    printf 'Unknown stage: %s\nValid stages: %s\n' "$s" "${VEDA_STAGES[*]}" >&2
    exit 2
}
[[ -n "$VEDA_FROM" ]] && validate_stage "$VEDA_FROM"
[[ -n "$VEDA_ONLY" ]] && validate_stage "$VEDA_ONLY"

# ---------------------------------------------------------------------------
# Logging, then the libraries
# ---------------------------------------------------------------------------
mkdir -p "$VEDA_LOG_DIR" "$VEDA_STATE" "$VEDA_STAGING"
VEDA_LOG="$VEDA_LOG_DIR/install-$(date '+%Y%m%d-%H%M%S').log"
: >"$VEDA_LOG"
ln -sfn "$VEDA_LOG" "$VEDA_LOG_DIR/install-latest.log" 2>/dev/null || true

if [[ ! -d "$VEDA_INSTALLER_DIR/lib" ]]; then
    cat >&2 <<EOF

FATAL: install.sh is not a standalone script.

It needs the installer/ directory next to it, and the VEDA application
itself. A complete checkout looks like this:

    $(basename "$VEDA_APP_DIR")/
    ├── install.sh              <- you ran this
    ├── veda-doctor
    ├── veda-pack
    ├── installer/              <- MISSING
    │   ├── lib/*.sh
    │   ├── envs/*.yml
    │   └── manifest/tools.manifest
    ├── src/                    <- the application
    └── resources/

Looked for : $VEDA_INSTALLER_DIR/lib
Ran from   : $VEDA_APP_DIR

If you copied just install.sh, copy the whole project instead:

    cp -a /path/to/IEL_pipeline/. $VEDA_APP_DIR/

or clone the repository and run ./install.sh from inside the clone.

EOF
    exit 1
fi

for _lib in common preflight probe system mamba envs mirror tools dockerize configure launcher verify; do
    _path="$VEDA_INSTALLER_DIR/lib/$_lib.sh"
    if [[ ! -f "$_path" ]]; then
        printf '\nFATAL: installer library missing: %s\n' "$_path" >&2
        printf 'The installer/ directory is incomplete. Re-copy or re-clone the project.\n\n' >&2
        exit 1
    fi
    # shellcheck source=/dev/null
    . "$_path"
done

trap 'veda::on_error $? $LINENO "${BASH_SOURCE[0]}" "${FUNCNAME[0]:-main}"' ERR

export VEDA_APP_DIR VEDA_INSTALLER_DIR VEDA_TOOLS_DIR VEDA_HOME VEDA_MAMBA_ROOT \
       VEDA_MAMBA_BIN VEDA_STATE VEDA_STAGING VEDA_LOG VEDA_VERBOSE VEDA_FORCE \
       VEDA_ASSUME_YES VEDA_DRY_RUN VEDA_SKIP_DOCKER VEDA_SKIP_SYSTEM VEDA_CPU_ONLY \
       VEDA_OFFLINE VEDA_ONLY VEDA_ONLY_TOOL VEDA_MIRROR

[[ "$VEDA_NO_MIRROR" == 1 ]] && VEDA_MIRROR=''

# ---------------------------------------------------------------------------
# Stage dispatch
# ---------------------------------------------------------------------------
# --from X: clear the markers for X and everything after it.
if [[ -n "$VEDA_FROM" ]]; then
    _hit=0
    for _s in "${VEDA_STAGES[@]}"; do
        [[ "$_s" == "$VEDA_FROM" ]] && _hit=1
        (( _hit )) && clear_stage "$_s"
    done
fi

stage() {
    local name=$1 fn=$2
    VEDA_CURRENT_STAGE=$name
    if ! should_run "$name"; then
        [[ -n "$VEDA_ONLY" && "$VEDA_ONLY" != "$name" ]] || log_skip "stage: $name"
        return 0
    fi
    # Under --dry-run only the read-only checks actually run. The rest would
    # have to half-execute to report anything useful (a dry "create env" leaves
    # no env for the next step to inspect), so announce and move on.
    if [[ "$VEDA_DRY_RUN" == 1 && "$name" != preflight ]]; then
        stage_header "$name"
        log "${C_DIM}would run stage:${C_RESET} $name"
        return 0
    fi
    "$fn"
    mark_stage_done "$name"
}

main() {
    banner "VEDA installer"
    log "project : $VEDA_APP_DIR"
    log "home    : $VEDA_HOME"
    log "log     : $VEDA_LOG"
    [[ "$VEDA_DRY_RUN" == 1 ]] && log_warn "dry run: nothing will be changed"
    [[ "$VEDA_OFFLINE" == 1 ]] && log_warn "offline: no downloads will be attempted"

    stage preflight veda::preflight
    stage probe     veda::probe
    stage system    veda::system_deps
    stage mamba     veda::mamba_install
    stage envs      veda::envs_create
    stage tools     veda::tools_install
    stage docker    veda::docker_setup
    stage configure veda::configure
    stage launcher  veda::launcher_install

    if [[ "$VEDA_PREWARM" == 1 ]]; then
        VEDA_CURRENT_STAGE=prewarm
        veda::envs_prewarm_models
    fi

    local verify_rc=0
    if [[ "$VEDA_SKIP_VERIFY" == 1 ]]; then
        log_info "--skip-verify given; run ./veda-doctor later to check the install"
    elif [[ -z "$VEDA_ONLY" || "$VEDA_ONLY" == verify ]]; then
        VEDA_CURRENT_STAGE=verify
        veda::verify || verify_rc=1
        (( verify_rc == 0 )) && mark_stage_done verify
    fi

    veda::_final_summary "$verify_rc"
    return "$verify_rc"
}

veda::_final_summary() {
    local verify_rc=$1

    # A single-stage run has not installed anything; don't claim it has.
    if [[ -n "$VEDA_ONLY" ]]; then
        printf '\n'
        if (( verify_rc == 0 )); then
            log_ok "stage '$VEDA_ONLY' complete"
        else
            log_err "stage '$VEDA_ONLY' reported problems"
        fi
        printf '   log: %s\n\n' "$VEDA_LOG"
        return 0
    fi

    if (( verify_rc == 0 )); then
        banner "VEDA is installed"
    else
        banner "VEDA is installed, with problems"
    fi

    local cmd=${VEDA_CMD:-veda}
    printf '\n   %sStart VEDA:%s\n' "$C_BOLD" "$C_RESET"
    if [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
        printf '       %s\n' "$cmd"
    else
        printf '       %s/bin/%s          %s(or open a new terminal and just: %s)%s\n' \
            "$VEDA_HOME" "$cmd" "$C_DIM" "$cmd" "$C_RESET"
    fi

    printf '\n   %sOther commands:%s\n' "$C_BOLD" "$C_RESET"
    printf '       %s doctor            re-check every tool\n' "$cmd"
    printf '       %s paths             show resolved interpreter/tool paths\n' "$cmd"
    printf '       ./install.sh --help    installer options\n'

    if (( ${#VEDA_TOOLS_MISSING[@]} > 0 )); then
        printf '\n   %sNot installed:%s %s\n' "$C_YELLOW$C_BOLD" "$C_RESET" "${VEDA_TOOLS_MISSING[*]}"
        printf '   These need a personal academic download. Stage the archives in\n'
        printf '       %s\n' "$VEDA_STAGING"
        printf '   and re-run ./install.sh -- everything else keeps working meanwhile.\n'
    fi

    if (( VEDA_DOCKER_NEEDS_RELOGIN )); then
        printf '\n   %sOne more step:%s you were added to the '"'"'docker'"'"' group.\n' "$C_YELLOW$C_BOLD" "$C_RESET"
        printf '   Log out and back in (or run: newgrp docker) before using Vaxign-ML.\n'
    fi

    if (( verify_rc != 0 )); then
        printf '\n   %sSome checks failed.%s Details above and in:\n       %s\n' \
            "$C_RED$C_BOLD" "$C_RESET" "$VEDA_LOG"
    fi
    printf '\n'
}

# A failing verification is a reportable outcome, not an installer crash, so
# keep it out of the ERR trap and just propagate the status.
_rc=0
main "$@" || _rc=$?
exit "$_rc"
