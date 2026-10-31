#!/usr/bin/env bash
# VEDA installer :: stage 2 -- apt packages the pipeline needs outside conda.
#
# Why each one is here:
#   tcsh          NetMHCpan / NetMHCIIpan / GibbsCluster wrappers are #!/bin/tcsh
#   perl          MERCI_motif_locator.pl (AlgPred2, ToxinPred2, IL2Pred)
#   git, git-lfs  the Raghava-lab repos keep their RF models in git-lfs
#   unzip         ifnepitope2 model.zip, il2pred-main.zip
#   bzip2         micromamba tarball
#   curl, wget    downloads
#   ca-certificates  TLS for all of the above
#   build-essential  source builds in pip fallbacks
#   libgl1, libglib2.0-0  matplotlib/opencv-style shared libs used by plotting
#   gnuplot       optional; Seq2Logo can emit plots through it

# The package LIST no longer lives here.
#
# Package names are not stable across Ubuntu releases -- 24.04's 64-bit time_t
# transition renamed libglib2.0-0 to libglib2.0-0t64, and by 26.04 the old
# name has no install candidate at all. A hardcoded list is therefore a list
# of names that are correct on exactly one release.
#
# installer/lib/probe.sh declares what each package is NEEDED FOR (a
# "capability") together with the candidate names that provide it, and resolves
# them against the running system's apt. This stage just installs whatever the
# probe resolved.
#
#   VEDA_PROBE_APT_RESOLVED   required, resolved for this release
#   VEDA_PROBE_APT_OPTIONAL   nice to have; failure never aborts
#   VEDA_PROBE_APT_PRESENT    the subset already installed

veda::system_deps() {
    # Works when this stage runs on its own (./install.sh --only system).
    veda::probe_ensure

    stage_header "System packages"

    if [[ "${VEDA_SKIP_SYSTEM:-0}" == 1 ]]; then
        log_info "--skip-system given; checking only that essentials exist"
        veda::_sys_verify_minimum
        return 0
    fi

    if ! have apt-get; then
        log_warn "apt-get not found; skipping system package installation"
        veda::_sys_verify_minimum
        return 0
    fi

    # Works even when preflight was skipped on a resumed run.
    veda::ensure_sudo

    if [[ "${VEDA_HAVE_SUDO:-0}" != 1 ]]; then
        log_warn "no usable sudo; skipping system package installation"
        veda::_sys_verify_minimum
        return 0
    fi

    veda::_sys_apt_install
    veda::_sys_git_lfs_init
    veda::_sys_verify_minimum
}

veda::_sys_apt_install() {
    if (( ${#VEDA_PROBE_APT_RESOLVED[@]} == 0 )); then
        log_warn "the probe resolved no apt packages; skipping"
        return 0
    fi

    # Work out what is genuinely missing so a re-run is quick and quiet.
    local want=() p
    for p in "${VEDA_PROBE_APT_RESOLVED[@]}"; do
        if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed'; then
            log_debug "apt: $p already installed"
        else
            want+=("$p")
        fi
    done

    if (( ${#want[@]} == 0 )); then
        log_ok "all required system packages already installed"
    else
        log_info "installing ${#want[@]} package(s): ${want[*]}"
        retry 2 "apt-get update" sudo apt-get update -y \
            || log_warn "apt-get update failed; trying the install with the current index"
        run "apt-get install" sudo env DEBIAN_FRONTEND=noninteractive \
            apt-get install -y --no-install-recommends "${want[@]}" \
            || die "could not install system packages: ${want[*]}
        Fix apt (e.g. 'sudo apt-get update') and re-run, or install them by
        hand and re-run with --skip-system."
        log_ok "system packages installed"
    fi

    # Optional extras: try, shrug off failure.
    local opt_missing=()
    for p in "${VEDA_PROBE_APT_OPTIONAL[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed' || opt_missing+=("$p")
    done
    if (( ${#opt_missing[@]} > 0 )); then
        log_debug "optional packages: ${opt_missing[*]}"
        if run "optional packages" sudo env DEBIAN_FRONTEND=noninteractive \
                apt-get install -y --no-install-recommends "${opt_missing[@]}"; then
            log_ok "optional packages installed (${opt_missing[*]})"
        else
            log_warn "optional packages unavailable (${opt_missing[*]}); continuing"
        fi
    fi
}

veda::_sys_git_lfs_init() {
    have git-lfs || { log_warn "git-lfs missing; large model files may download as pointer stubs"; return 0; }
    # Per-user hook install; harmless if already done.
    if run "git lfs install" git lfs install --skip-repo; then
        log_ok "git-lfs initialised"
    else
        log_warn "git lfs install failed; model downloads may be incomplete"
    fi
}

veda::_sys_verify_minimum() {
    # These are hard requirements -- the pipeline cannot run without them.
    local fatal=() warn=()

    have tcsh || fatal+=("tcsh (needed by NetMHCpan, NetMHCIIpan, GibbsCluster)")
    have perl || fatal+=("perl (needed by MERCI_motif_locator.pl)")
    have git  || warn+=("git (needed to download the Raghava-lab tools)")
    have unzip || warn+=("unzip (needed to unpack bundled model archives)")
    have git-lfs || warn+=("git-lfs (model files will download as stubs)")

    local item
    for item in "${warn[@]}"; do log_warn "missing: $item"; done

    if (( ${#fatal[@]} > 0 )); then
        log_err "the following are required and still missing:"
        for item in "${fatal[@]}"; do log_err "  - $item"; done
        die "install them with:  sudo apt-get install -y tcsh perl
        then re-run ./install.sh"
    fi

    log_ok "required system interpreters present (tcsh, perl)"
}
