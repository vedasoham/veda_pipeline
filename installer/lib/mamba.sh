#!/usr/bin/env bash
# VEDA installer :: stage 3 -- private micromamba.
#
# VEDA deliberately does NOT use whatever conda/anaconda the user already has.
# A shared base gets upgraded by unrelated projects and silently breaks the
# version pins these predictors depend on. Instead we drop a single static
# micromamba binary into $VEDA_HOME/bin and keep all four environments under
# $VEDA_HOME/mamba. Uninstalling VEDA is then `rm -rf ~/.veda`.

VEDA_MAMBA_URL_PRIMARY="https://micro.mamba.pm/api/micromamba/linux-64/latest"
VEDA_MAMBA_URL_FALLBACK="https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-linux-64"

veda::mamba_install() {
    stage_header "Package manager (micromamba)"

    mkdir -p "$VEDA_HOME/bin" "$VEDA_MAMBA_ROOT"

    if [[ -x "$VEDA_MAMBA_BIN" ]] && "$VEDA_MAMBA_BIN" --version >/dev/null 2>&1; then
        log_ok "micromamba already installed ($("$VEDA_MAMBA_BIN" --version 2>/dev/null)) at $VEDA_MAMBA_BIN"
        veda::_mamba_write_condarc
        return 0
    fi

    log_info "downloading micromamba into $VEDA_HOME/bin"
    local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/veda-mamba.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN

    if veda::_mamba_try_tarball "$tmp"; then
        log_ok "micromamba installed from the mamba.pm tarball"
    elif veda::_mamba_try_binary "$tmp"; then
        log_ok "micromamba installed from the GitHub release"
    else
        die "could not download micromamba.
        Tried:
          $VEDA_MAMBA_URL_PRIMARY
          $VEDA_MAMBA_URL_FALLBACK
        Check your network/proxy, or place a micromamba binary at
        $VEDA_MAMBA_BIN yourself and re-run."
    fi

    chmod +x "$VEDA_MAMBA_BIN"
    "$VEDA_MAMBA_BIN" --version >>"$VEDA_LOG" 2>&1 \
        || die "the downloaded micromamba will not run; the file may be truncated.
        Delete $VEDA_MAMBA_BIN and re-run."

    log_ok "micromamba $("$VEDA_MAMBA_BIN" --version 2>/dev/null) ready"
    veda::_mamba_write_condarc
}

veda::_mamba_try_tarball() {
    local tmp=$1
    have curl || return 1
    retry 3 "fetch micromamba tarball" \
        curl -fsSL --connect-timeout 20 -o "$tmp/mm.tar.bz2" "$VEDA_MAMBA_URL_PRIMARY" || return 1
    # The archive holds bin/micromamba; -j needs bzip2, checked in preflight.
    run "unpack micromamba" tar -xjf "$tmp/mm.tar.bz2" -C "$tmp" bin/micromamba || return 1
    [[ -f "$tmp/bin/micromamba" ]] || return 1
    mv -f "$tmp/bin/micromamba" "$VEDA_MAMBA_BIN"
}

veda::_mamba_try_binary() {
    local tmp=$1
    local fetch
    if have curl; then fetch=(curl -fsSL --connect-timeout 20 -o "$tmp/micromamba")
    elif have wget; then fetch=(wget -q -O "$tmp/micromamba")
    else return 1
    fi
    retry 3 "fetch micromamba binary" "${fetch[@]}" "$VEDA_MAMBA_URL_FALLBACK" || return 1
    [[ -s "$tmp/micromamba" ]] || return 1
    mv -f "$tmp/micromamba" "$VEDA_MAMBA_BIN"
}

# A root-prefix-local .condarc keeps channel order deterministic and turns off
# the base auto-activation that confuses subprocess PATH handling.
veda::_mamba_write_condarc() {
    atomic_write "$VEDA_MAMBA_ROOT/.condarc" <<'EOF'
# Written by the VEDA installer. Scoped to this root prefix only.
channels:
  - conda-forge
  - bioconda
channel_priority: strict
auto_activate_base: false
always_yes: true
EOF
    log_debug "wrote $VEDA_MAMBA_ROOT/.condarc"
}

# mamba <args...>  -- micromamba with VEDA's root prefix always applied.
mamba() {
    MAMBA_ROOT_PREFIX="$VEDA_MAMBA_ROOT" \
    CONDARC="$VEDA_MAMBA_ROOT/.condarc" \
    "$VEDA_MAMBA_BIN" --root-prefix "$VEDA_MAMBA_ROOT" "$@"
}

# env_prefix <name> / env_python <name> -- paths inside the private root.
env_prefix() { printf '%s/envs/%s' "$VEDA_MAMBA_ROOT" "$1"; }
env_python() { printf '%s/envs/%s/bin/python' "$VEDA_MAMBA_ROOT" "$1"; }
env_bin()    { printf '%s/envs/%s/bin/%s' "$VEDA_MAMBA_ROOT" "$1" "$2"; }

env_exists() { [[ -x "$(env_python "$1")" ]]; }
