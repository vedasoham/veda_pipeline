#!/usr/bin/env bash
# VEDA installer :: stage 1 -- check the machine before touching anything.
#
# Everything here is read-only. The goal is that a machine which cannot
# complete the install fails in this stage, with an actionable message,
# rather than halfway through a 20-minute conda solve.

# Rough disk budget, in GiB, for a complete install:
#   conda envs (torch+CUDA dominates) ~12   docker images ~6
#   ESM/T5 model cache for DeepLoc    ~3    bundled tools  ~2
VEDA_DISK_NEED_GIB=24
VEDA_DISK_WARN_GIB=30

veda::preflight() {
    stage_header "Pre-flight checks"

    veda::_pf_not_root
    veda::_pf_os
    veda::_pf_arch
    veda::_pf_layout
    veda::_pf_base_tools
    veda::_pf_disk
    veda::_pf_memory
    veda::_pf_network
    veda::_pf_sudo

    log_ok "pre-flight checks passed"
}

# --- individual checks ------------------------------------------------------

veda::_pf_not_root() {
    if [[ ${EUID} -eq 0 ]]; then
        die "do not run install.sh as root or with sudo.
        VEDA installs into your home directory and must be owned by your
        own user. The script asks for sudo only for the few apt/docker
        steps that need it.
        Re-run as: ./install.sh"
    fi
    log_ok "running as unprivileged user '$USER'"
}

veda::_pf_os() {
    [[ -r /etc/os-release ]] || die "cannot read /etc/os-release; unsupported system"
    # shellcheck disable=SC1091
    . /etc/os-release

    VEDA_OS_ID=${ID:-unknown}
    VEDA_OS_LIKE=${ID_LIKE:-}
    VEDA_OS_VER=${VERSION_ID:-unknown}
    VEDA_OS_NAME=${PRETTY_NAME:-$VEDA_OS_ID}

    case "$VEDA_OS_ID" in
        ubuntu|debian|linuxmint|pop|elementary|zorin|raspbian) : ;;
        *)
            if [[ "$VEDA_OS_LIKE" == *debian* ]]; then
                log_warn "$VEDA_OS_NAME is not explicitly supported but looks Debian-like; continuing"
            else
                log_err "this installer targets Ubuntu/Debian; detected: $VEDA_OS_NAME"
                confirm "Continue anyway (apt steps will likely fail)?" n \
                    || die "stopping at your request"
            fi
            ;;
    esac
    log_ok "operating system: $VEDA_OS_NAME"

    if grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; then
        VEDA_IS_WSL=1
        log_warn "WSL detected. Keep the project on the Linux filesystem (~/), not /mnt/c,"
        log_warn "or BLAST and the DTU binaries will run many times slower."
    else
        VEDA_IS_WSL=0
    fi
}

veda::_pf_arch() {
    local arch; arch=$(uname -m)
    VEDA_ARCH=$arch
    if [[ "$arch" != x86_64 ]]; then
        die "the bundled NetMHCpan / NetMHCIIpan / GibbsCluster binaries are
        Linux_x86_64 only, but this machine is '$arch'.
        Those three tools cannot run here. Re-download native builds from
        DTU Health Tech, or run VEDA on an x86_64 host."
    fi
    log_ok "architecture: $arch"
}

veda::_pf_layout() {
    [[ -f "$VEDA_APP_DIR/src/config.py" ]] \
        || die "src/config.py not found under $VEDA_APP_DIR.
        Run install.sh from the VEDA project directory."
    [[ -f "$VEDA_APP_DIR/src/main.py" ]] \
        || die "src/main.py not found; this does not look like a VEDA checkout"
    [[ -d "$VEDA_TOOLS_DIR" ]] || {
        log_warn "resources/tools/ does not exist yet; it will be created"
        mkdir -p "$VEDA_TOOLS_DIR"
    }
    log_ok "project layout looks right ($VEDA_APP_DIR)"

    # A space in the path breaks the tcsh wrappers and several of the
    # Perl/Python tools that build command lines by string concatenation.
    if [[ "$VEDA_APP_DIR" == *' '* ]]; then
        die "the project path contains a space:
            $VEDA_APP_DIR
        NetMHCpan's tcsh wrapper and MERCI_motif_locator.pl both break on
        spaces. Move the project somewhere without spaces and re-run."
    fi
}

veda::_pf_base_tools() {
    local missing=()
    for c in bash sed awk grep find tar date df; do
        have "$c" || missing+=("$c")
    done
    (( ${#missing[@]} == 0 )) \
        || die "core utilities missing: ${missing[*]} -- this system is too minimal to proceed"

    # bash 4+ for associative arrays and ${var,,}
    if (( BASH_VERSINFO[0] < 4 )); then
        die "bash 4 or newer required (found ${BASH_VERSION})"
    fi
    log_ok "shell and core utilities present (bash ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]})"
}

veda::_pf_disk() {
    local kib gib_avail
    kib=$(free_kib "$VEDA_HOME")
    gib_avail=$(awk -v k="$kib" 'BEGIN{printf "%d", k/1048576}')

    log_info "free space on $(df -P "$(dirname "$VEDA_HOME")" | awk 'NR==2{print $6}'): $(human_gib "$kib")"

    if (( gib_avail < VEDA_DISK_NEED_GIB )); then
        log_err "need about ${VEDA_DISK_NEED_GIB} GiB free, found ${gib_avail} GiB"
        log_err "the conda environments alone are roughly 12 GiB (PyTorch + CUDA)"
        log_err "free some space, or install without Docker/GPU:  ./install.sh --skip-docker --cpu-only"
        confirm "Continue regardless?" n || die "stopping: not enough disk space"
    elif (( gib_avail < VEDA_DISK_WARN_GIB )); then
        log_warn "only ${gib_avail} GiB free; the install will fit but leaves little headroom"
        log_warn "VEDA writes results under data/ and caches models under $VEDA_HOME"
    else
        log_ok "disk space sufficient (${gib_avail} GiB free)"
    fi
}

veda::_pf_memory() {
    local mem_kib mem_gib
    mem_kib=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
    mem_gib=$(awk -v k="$mem_kib" 'BEGIN{printf "%d", k/1048576}')
    if (( mem_gib == 0 )); then
        log_warn "could not read total memory; skipping check"
        return 0
    fi
    if (( mem_gib < 8 )); then
        log_warn "${mem_gib} GiB RAM detected. DeepLoc2 and DeepTMHMM want 8 GiB+;"
        log_warn "large FASTA inputs may be killed by the OOM reaper."
    else
        log_ok "memory: ${mem_gib} GiB"
    fi
}

veda::_pf_network() {
    if [[ "${VEDA_OFFLINE:-0}" == 1 ]]; then
        log_warn "offline mode: skipping connectivity check"
        return 0
    fi
    local host ok=0
    for host in github.com conda.anaconda.org pypi.org; do
        if timeout 8 bash -c ">/dev/tcp/$host/443" 2>/dev/null; then
            log_debug "reached $host:443"
            ok=$(( ok + 1 ))
        else
            log_warn "cannot reach $host:443"
        fi
    done
    if (( ok == 0 )); then
        log_err "no outbound HTTPS connectivity to GitHub, conda or PyPI"
        log_err "the installer must download micromamba, packages and tools"
        log_err "if you are behind a proxy, export https_proxy before re-running"
        die "stopping: no network"
    fi
    log_ok "network reachable ($ok/3 endpoints)"
}

veda::_pf_sudo() { veda::ensure_sudo; }

# Determine once whether we can use sudo, and remember the answer.
#
# This lives outside veda::preflight on purpose. VEDA_HAVE_SUDO is runtime
# state, but preflight is a skippable stage -- on a resumed run (or with
# `--only system`) it never executes, and the system stage would then see
# VEDA_HAVE_SUDO=0 and silently skip every apt package. Any stage that needs
# root calls this first instead of trusting the variable.
veda::ensure_sudo() {
    [[ "${VEDA_SUDO_CHECKED:-0}" == 1 ]] && return 0
    VEDA_SUDO_CHECKED=1

    if [[ "${VEDA_SKIP_SYSTEM:-0}" == 1 ]]; then
        log_info "--skip-system given; sudo not required"
        VEDA_HAVE_SUDO=0
        return 0
    fi
    if ! have sudo; then
        log_warn "sudo not installed; system packages cannot be installed automatically"
        VEDA_HAVE_SUDO=0
        return 0
    fi
    if sudo -n true 2>/dev/null; then
        log_ok "sudo available (cached credentials)"
        VEDA_HAVE_SUDO=1
        return 0
    fi

    log_info "Installing system packages (tcsh, perl, git-lfs, ...) needs your"
    log_info "password once. Everything after that is user-level."
    if [[ ! -t 0 ]]; then
        log_warn "no terminal available and sudo needs a password"
        log_warn "system packages will be SKIPPED -- re-run interactively, or"
        log_warn "pre-authorise with 'sudo -v' before an unattended run"
        VEDA_HAVE_SUDO=0
        return 0
    fi
    if sudo -v; then
        log_ok "sudo authenticated"
        VEDA_HAVE_SUDO=1
    else
        log_warn "sudo authentication failed; system packages will be skipped"
        VEDA_HAVE_SUDO=0
    fi
}
