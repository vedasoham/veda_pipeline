#!/usr/bin/env bash
#
#  VEDA uninstaller
#
#  Removes what the installer created. Nothing is deleted without telling you
#  what it is and asking first.
#
#  Usage:  ./uninstall.sh [--all] [--yes]
#
#    (default)   environments, launcher, desktop entry, generated config.
#                Your tools, data and results are kept.
#    --all       also delete ~/.veda entirely (model caches, logs, staging)
#                and the generated veda_paths.json.
#
#  Never touched: resources/tools/ (the tools themselves, several GB and in
#  part licence-gated), data/, logs/, and your project files. Delete those by
#  hand if you really mean to.
#
set -Eeuo pipefail

VEDA_SELF=${BASH_SOURCE[0]}
while [[ -L "$VEDA_SELF" ]]; do
    _link=$(readlink "$VEDA_SELF")
    [[ "$_link" == /* ]] && VEDA_SELF=$_link || VEDA_SELF=$(dirname "$VEDA_SELF")/$_link
done
VEDA_APP_DIR=$(cd "$(dirname "$VEDA_SELF")" && pwd -P)
VEDA_INSTALLER_DIR="$VEDA_APP_DIR/installer"
VEDA_TOOLS_DIR="$VEDA_APP_DIR/resources/tools"
VEDA_HOME=${VEDA_HOME:-$HOME/.veda}
VEDA_CMD=$(basename "$VEDA_HOME"); VEDA_CMD=${VEDA_CMD#.}; [[ -z "$VEDA_CMD" ]] && VEDA_CMD=veda

REMOVE_ALL=0
VEDA_ASSUME_YES=0
VEDA_DRY_RUN=0
VEDA_VERBOSE=0
VEDA_LOG=""

while (( $# )); do
    case $1 in
        --all)      REMOVE_ALL=1 ;;
        -y|--yes)   VEDA_ASSUME_YES=1 ;;
        -h|--help)  sed -n '2,19p' "$VEDA_SELF" | sed 's/^#\s\?//'; exit 0 ;;
        *) printf 'Unknown option: %s (try --help)\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

# shellcheck source=/dev/null
. "$VEDA_INSTALLER_DIR/lib/common.sh"

banner "VEDA uninstaller"

# --- show the plan ----------------------------------------------------------
printf '\n   %sWill remove:%s\n' "$C_BOLD" "$C_RESET"
[[ -d "$VEDA_HOME/mamba" ]] && printf '     %s  (python environments)\n' "$VEDA_HOME/mamba"
[[ -e "$VEDA_HOME/bin/$VEDA_CMD" ]] && printf '     %s/bin/%s\n' "$VEDA_HOME" "$VEDA_CMD"
[[ -L "$HOME/.local/bin/$VEDA_CMD" ]] && printf '     %s/.local/bin/%s\n' "$HOME" "$VEDA_CMD"
[[ -f "$HOME/.local/share/applications/$VEDA_CMD.desktop" ]] && \
    printf '     %s/.local/share/applications/%s.desktop\n' "$HOME" "$VEDA_CMD"
if (( REMOVE_ALL )); then
    printf '     %s  (everything: caches, logs, staging, state)\n' "$VEDA_HOME"
    printf '     %s/veda_paths.json\n' "$VEDA_APP_DIR"
fi

printf '\n   %sWill KEEP:%s\n' "$C_BOLD" "$C_RESET"
printf '     %s  (the prediction tools)\n' "$VEDA_TOOLS_DIR"
printf '     %s/data  and  %s/logs  (your projects and results)\n' "$VEDA_APP_DIR" "$VEDA_APP_DIR"
(( REMOVE_ALL )) || printf '     %s  (caches, logs, staging -- use --all to remove)\n' "$VEDA_HOME"
printf '     Docker images (remove with: docker rmi e4ong1031/vaxign-ml biolib/dtu-deeptmhmm)\n'
printf '     System packages installed via apt (tcsh, perl, git-lfs, docker)\n'
printf '     The PATH line in your shell rc, if you accepted it\n'
printf '\n'

confirm "Proceed?" n || { log_info "nothing removed"; exit 0; }

# --- do it ------------------------------------------------------------------
if (( REMOVE_ALL )); then
    if [[ -d "$VEDA_HOME" ]]; then
        rm -rf "$VEDA_HOME"
        log_ok "removed $VEDA_HOME"
    fi
    if [[ -f "$VEDA_APP_DIR/veda_paths.json" ]]; then
        rm -f "$VEDA_APP_DIR/veda_paths.json"
        log_ok "removed veda_paths.json"
    fi
else
    if [[ -d "$VEDA_HOME/mamba" ]]; then
        rm -rf "$VEDA_HOME/mamba"
        log_ok "removed the python environments"
    fi
    rm -f "$VEDA_HOME/bin/$VEDA_CMD"
    rm -f "$VEDA_HOME/state/"*.done 2>/dev/null || true
    log_ok "removed the launcher and stage markers"
fi

rm -f "$HOME/.local/bin/$VEDA_CMD"
rm -f "$HOME/.local/share/applications/$VEDA_CMD.desktop"
have update-desktop-database && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
log_ok "removed the launcher symlink and desktop entry"

# --- offer to restore patched tool files ------------------------------------
mapfile -t origs < <(find "$VEDA_TOOLS_DIR" -maxdepth 2 -name '*.veda-orig' 2>/dev/null || true)
if (( ${#origs[@]} > 0 )); then
    printf '\n   The installer patched %d tool file(s) and kept the originals:\n' "${#origs[@]}"
    for f in "${origs[@]}"; do printf '     %s\n' "${f#"$VEDA_TOOLS_DIR"/}"; done
    printf '\n'
    if confirm "Restore them to their original (pre-install) contents?" n; then
        for f in "${origs[@]}"; do
            mv -f "$f" "${f%.veda-orig}"
            log_ok "restored ${f%.veda-orig}"
        done
    else
        log_info "left the patched files in place"
    fi
fi

printf '\n'
log_ok "VEDA uninstalled"
printf '   Reinstall any time with:  %s/install.sh\n\n' "$VEDA_APP_DIR"
