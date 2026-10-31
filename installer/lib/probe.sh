#!/usr/bin/env bash
# VEDA installer :: stage 1b -- system probe.
#
# This stage installs nothing. It discovers what the machine already has,
# records it, and sets the VEDA_PROBE_* variables that the later stages use
# instead of each re-detecting things for itself.
#
# It exists because "works on the developer's box" is not the same as
# "works on an Ubuntu box". The variance this stage is written to absorb:
#
#   * Distro release. Package names move between releases (Ubuntu 24.04's
#     64-bit time_t transition renamed libglib2.0-0 to libglib2.0-0t64, and
#     on 26.04 the old name has no install candidate at all). Hardcoding
#     either name is wrong; we resolve names at runtime.
#   * An existing Anaconda/Miniconda/Miniforge. We never touch it, but an
#     ACTIVATED one exports variables that leak into every subprocess we
#     launch and silently redirect Python, Perl, TLS and the linker.
#   * A GPU that may be absent, present without a driver, or present with a
#     driver too old for the CUDA wheels we would otherwise choose.
#   * pip that may be missing, PEP-668 "externally managed", or configured to
#     redirect installs to ~/.local.
#   * Filesystems that cannot execute, or cannot hold symlinks.
#
# Anaconda and the NVIDIA driver are deliberately NOT installed by this
# installer -- both are system-wide, user-owned decisions. We detect, report,
# and adapt.

# --------------------------------------------------------------------------
# Environment variables that must not reach our subprocesses.
#
# Used in two places: the report below, and the generated launcher, which
# emits an `unset` for each. Keeping one list means the two cannot drift.
#
# Why each is hostile to an isolated install:
#
#   PYTHONPATH          prepended to sys.path ahead of the env -- the single
#                       most common way a conda env ends up importing someone
#                       else's packages.
#   PYTHONHOME          relocates the stdlib; a wrong value breaks startup.
#   PYTHONSTARTUP       runs arbitrary code in interactive interpreters.
#   PYTHONUSERBASE      moves the --user tree we are trying to ignore.
#   CONDA_PREFIX        several tools treat it as "the active environment"
#   CONDA_DEFAULT_ENV   and resolve siblings relative to it.
#   CONDA_SHLVL
#   CONDA_PROMPT_MODIFIER
#   CONDA_PYTHON_EXE
#   _CE_CONDA / _CE_M    conda's own shell-function bookkeeping.
#   PERL5LIB            MERCI_motif_locator.pl is Perl. Anaconda ships a Perl
#                       with its own module tree; inheriting PERL5LIB makes
#                       the system perl load Anaconda's modules.
#   PERL_MM_OPT / PERL_MB_OPT  force install prefixes onto Perl builds.
#   SSL_CERT_FILE       if these point into Anaconda's cacert.pem, every HTTPS
#   SSL_CERT_DIR        call from our envs depends on Anaconda staying put.
#   REQUESTS_CA_BUNDLE  Each env ships its own certifi; let it use it.
#   CURL_CA_BUNDLE
#   PIP_USER            redirects installs to ~/.local.
#   PIP_TARGET          redirects installs to an arbitrary directory.
#   PIP_PREFIX
#   PIP_REQUIRE_VIRTUALENV  makes pip refuse to run outside a venv.
#   PYTHONWARNINGS      can turn warnings into errors inside the workers.
#
# LD_LIBRARY_PATH is handled separately -- it is filtered, not unset, because
# a user may legitimately need entries in it.
VEDA_SCRUB_VARS=(
    PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PYTHONWARNINGS
    CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER
    CONDA_PYTHON_EXE CONDA_EXE _CE_CONDA _CE_M
    PERL5LIB PERL_MM_OPT PERL_MB_OPT
    SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
    PIP_USER PIP_TARGET PIP_PREFIX PIP_REQUIRE_VIRTUALENV
)

# Capability -> candidate package names, most-preferred first.
#
# Resolved at runtime by veda::_apt_pick. A capability with several candidates
# is one whose package name differs across the releases we support; the
# comment says where it moved.
VEDA_APT_CAPS=(
    "ca-certificates|ca-certificates"
    "curl|curl"
    "wget|wget"
    "git|git"
    "git-lfs|git-lfs"
    "tcsh|tcsh"
    "perl|perl"
    "unzip|unzip"
    "bzip2|bzip2"
    "xz|xz-utils"
    "build|build-essential"
    "pkgconfig|pkg-config|pkgconf"
    "gnupg|gnupg|gnupg2"
    "lsb|lsb-release"
    # Ubuntu 24.04 renamed this for the 64-bit time_t transition; on 26.04 the
    # bare name has no candidate and survives only as a virtual package.
    "glib|libglib2.0-0t64|libglib2.0-0"
)

VEDA_APT_CAPS_OPTIONAL=(
    # libgl1-mesa-glx was dropped after 22.04; libgl1 is the modern name.
    "gl|libgl1|libgl1-mesa-glx"
    "gnuplot|gnuplot-nox|gnuplot"
)

# Run the probe unless it has already run in THIS process.
#
# Called at the top of every stage that depends on probe results, so that
# `./install.sh --only envs` works on its own. Deliberately keyed to the
# process and not to a state marker on disk: the answers must describe the
# machine as it is right now. A user who installs an NVIDIA driver and re-runs
# the installer must get CUDA wheels, not a cached "no GPU" verdict.
veda::probe_ensure() {
    [[ "${VEDA_PROBE_DONE:-0}" == 1 ]] && return 0
    veda::probe
}

veda::probe() {
    stage_header "System probe"

    VEDA_PROBE_HAZARDS=()
    VEDA_PROBE_NOTES=()

    veda::_probe_os
    veda::_probe_resources
    veda::_probe_filesystem
    veda::_probe_conda
    veda::_probe_python
    veda::_probe_env_hazards
    veda::_probe_gpu
    veda::_probe_container
    veda::_probe_apt

    veda::_probe_write_report

    VEDA_PROBE_DONE=1
    veda::_probe_summarise
}

# --- helpers ---------------------------------------------------------------

veda::_hazard() { VEDA_PROBE_HAZARDS+=("$1"); }
veda::_note()   { VEDA_PROBE_NOTES+=("$1"); }

# First candidate that is installed, else first that apt can actually install.
#
# The second loop uses `apt-get install -s`, not `apt-cache policy`, because
# policy reports "Candidate: (none)" for a purely virtual package while
# apt-get resolves it fine when exactly one package provides it. Simulation
# needs no root.
veda::_apt_pick() {
    local cand
    for cand in "$@"; do
        if dpkg-query -W -f='${Status}' "$cand" 2>/dev/null | grep -q 'ok installed'; then
            printf '%s' "$cand"; return 0
        fi
    done
    for cand in "$@"; do
        if apt-get install -s --no-install-recommends "$cand" >/dev/null 2>&1; then
            printf '%s' "$cand"; return 0
        fi
    done
    return 1
}

# --- OS --------------------------------------------------------------------

veda::_probe_os() {
    VEDA_PROBE_OS_ID=unknown
    VEDA_PROBE_OS_VER=unknown
    VEDA_PROBE_OS_CODENAME=unknown
    VEDA_PROBE_OS_LIKE=""
    VEDA_PROBE_OS_PRETTY="unknown"

    if [[ -r /etc/os-release ]]; then
        # Subshell: os-release defines NAME/VERSION/... and we do not want
        # those names in the installer's own scope.
        local line
        while IFS= read -r line; do
            case $line in
                __ID=*)       VEDA_PROBE_OS_ID=${line#__ID=} ;;
                __VER=*)      VEDA_PROBE_OS_VER=${line#__VER=} ;;
                __CODE=*)     VEDA_PROBE_OS_CODENAME=${line#__CODE=} ;;
                __LIKE=*)     VEDA_PROBE_OS_LIKE=${line#__LIKE=} ;;
                __PRETTY=*)   VEDA_PROBE_OS_PRETTY=${line#__PRETTY=} ;;
            esac
        done < <(
            . /etc/os-release 2>/dev/null || true
            printf '__ID=%s\n__VER=%s\n__CODE=%s\n__LIKE=%s\n__PRETTY=%s\n' \
                "${ID:-unknown}" "${VERSION_ID:-unknown}" \
                "${VERSION_CODENAME:-unknown}" "${ID_LIKE:-}" \
                "${PRETTY_NAME:-unknown}"
        )
    fi

    VEDA_PROBE_ARCH=$(uname -m 2>/dev/null || echo unknown)
    VEDA_PROBE_KERNEL=$(uname -r 2>/dev/null || echo unknown)
    VEDA_PROBE_GLIBC=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo unknown)

    VEDA_PROBE_IS_WSL=0
    if [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
        VEDA_PROBE_IS_WSL=1
    fi

    VEDA_PROBE_IS_CONTAINER=0
    if [[ -f /.dockerenv ]] || grep -qE '(docker|lxc|containerd)' /proc/1/cgroup 2>/dev/null; then
        VEDA_PROBE_IS_CONTAINER=1
    fi

    log_info "OS: $VEDA_PROBE_OS_PRETTY  ($VEDA_PROBE_ARCH, glibc $VEDA_PROBE_GLIBC)"

    # Debian-family is what the apt stage assumes. Anything else still gets a
    # full install attempt, but with the system stage reduced to a check.
    local family="$VEDA_PROBE_OS_ID $VEDA_PROBE_OS_LIKE"
    if [[ "$family" != *debian* && "$family" != *ubuntu* ]]; then
        veda::_hazard "not a Debian/Ubuntu system ($VEDA_PROBE_OS_ID); apt packages must be installed by hand"
    fi

    (( VEDA_PROBE_IS_WSL )) && veda::_note "running under WSL; Docker and GPU access depend on the Windows host"
    (( VEDA_PROBE_IS_CONTAINER )) && veda::_note "running inside a container; the docker stage will be skipped"

    # Every probe function ends with an explicit `return 0`.
    #
    # Not cosmetic. Under `set -e` with an ERR trap, a function whose LAST
    # command is `cond && action` returns 1 when cond is false, which aborts
    # the installer. A false condition here is the normal case -- most machines
    # are not containers -- so without this the probe would crash on almost
    # every system it was meant to make the installer safe on.
    return 0
}

# --- CPU / memory ----------------------------------------------------------

veda::_probe_resources() {
    VEDA_PROBE_CORES=$(nproc 2>/dev/null || echo 1)
    VEDA_PROBE_RAM_MIB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
    VEDA_PROBE_SWAP_MIB=$(awk '/SwapTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)

    # onnxruntime (DeepLoc2) and the PyTorch wheels are built assuming AVX2.
    # Without it they either crash with an illegal instruction or fall back to
    # a very slow path, so it is worth knowing before a 2.5 GB download.
    VEDA_PROBE_AVX2=0
    grep -qm1 '\bavx2\b' /proc/cpuinfo 2>/dev/null && VEDA_PROBE_AVX2=1
    (( VEDA_PROBE_AVX2 )) || veda::_hazard "CPU has no AVX2; PyTorch and onnxruntime may be very slow or crash"

    log_info "CPU: ${VEDA_PROBE_CORES} cores, AVX2=$( ((VEDA_PROBE_AVX2)) && echo yes || echo no ); RAM: $((VEDA_PROBE_RAM_MIB/1024)) GiB"

    (( VEDA_PROBE_RAM_MIB < 7500 )) &&
        veda::_hazard "only $((VEDA_PROBE_RAM_MIB/1024)) GiB RAM; ESM-2 in DeepLoc2 wants 8 GiB or more"

    return 0
}

# --- filesystem ------------------------------------------------------------

veda::_probe_filesystem() {
    mkdir -p "$VEDA_HOME" 2>/dev/null || true

    VEDA_PROBE_FSTYPE=$(findmnt -no FSTYPE --target "$VEDA_HOME" 2>/dev/null || echo unknown)
    VEDA_PROBE_TMP_FSTYPE=$(findmnt -no FSTYPE --target "${TMPDIR:-/tmp}" 2>/dev/null || echo unknown)

    # Test rather than parse mount flags: a noexec mount, a filesystem with no
    # exec bit (exFAT, NTFS, some NFS exports) and a restrictive mount option
    # all fail the same way, and all of them break a conda environment --
    # every binary in it lives under $VEDA_HOME.
    VEDA_PROBE_EXEC_OK=0
    local t="$VEDA_HOME/.probe-exec.$$"
    if printf '#!/bin/sh\nexit 0\n' >"$t" 2>/dev/null && chmod +x "$t" 2>/dev/null && "$t" 2>/dev/null; then
        VEDA_PROBE_EXEC_OK=1
    fi
    rm -f "$t" 2>/dev/null || true

    # Conda environments are full of symlinks.
    VEDA_PROBE_SYMLINK_OK=0
    local s="$VEDA_HOME/.probe-link.$$"
    if ln -s /dev/null "$s" 2>/dev/null; then VEDA_PROBE_SYMLINK_OK=1; fi
    rm -f "$s" 2>/dev/null || true

    log_info "install filesystem: $VEDA_PROBE_FSTYPE (exec=$( ((VEDA_PROBE_EXEC_OK)) && echo yes || echo NO ), symlinks=$( ((VEDA_PROBE_SYMLINK_OK)) && echo yes || echo NO ))"

    (( VEDA_PROBE_EXEC_OK )) ||
        veda::_hazard "cannot execute files under $VEDA_HOME ($VEDA_PROBE_FSTYPE, likely a noexec mount); choose another VEDA_HOME"
    (( VEDA_PROBE_SYMLINK_OK )) ||
        veda::_hazard "cannot create symlinks under $VEDA_HOME ($VEDA_PROBE_FSTYPE); conda environments require them"

    # The DTU binaries have a 64-byte buffer for the scratch directory path.
    # Measured against netMHCpan 4.2: a TMPDIR of 63 characters produces
    # predictions, 64 dies with "*** buffer overflow detected ***" and a core
    # dump. glibc's _FORTIFY_SOURCE turns the overrun into an abort, so on a
    # modern distro this is a hard crash rather than silent corruption.
    #
    # The launcher points TMPDIR at $VEDA_HOME/tmp, so the length of VEDA_HOME
    # decides whether NetMHCpan, NetMHCIIpan and GibbsCluster work at all.
    VEDA_PROBE_TMPDIR="$VEDA_HOME/tmp"
    VEDA_PROBE_TMPDIR_LEN=${#VEDA_PROBE_TMPDIR}
    if (( VEDA_PROBE_TMPDIR_LEN > 63 )); then
        veda::_hazard "VEDA_HOME is too deep: the scratch path '$VEDA_PROBE_TMPDIR' is $VEDA_PROBE_TMPDIR_LEN characters, and the DTU tools (NetMHCpan, NetMHCIIpan, GibbsCluster) crash above 63.
      Install with a shorter VEDA_HOME, for example:
          VEDA_HOME=~/.veda ./install.sh"
    else
        log_debug "scratch path length ${VEDA_PROBE_TMPDIR_LEN}/63 -- within the DTU tools' limit"
    fi

    return 0
}

# --- existing conda installations -----------------------------------------

veda::_probe_conda() {
    VEDA_PROBE_CONDA_ROOTS=()
    VEDA_PROBE_CONDA_VERSION=""
    VEDA_PROBE_CONDA_ACTIVE=""

    local d
    for d in "$HOME/anaconda3" "$HOME/miniconda3" "$HOME/miniforge3" \
             "$HOME/mambaforge" "$HOME/micromamba" "$HOME/conda" \
             /opt/conda /opt/anaconda3 /opt/miniconda3 \
             /usr/local/anaconda3 /usr/local/miniconda3; do
        [[ -x "$d/bin/conda" || -x "$d/bin/python" ]] && VEDA_PROBE_CONDA_ROOTS+=("$d")
    done

    if have conda; then
        VEDA_PROBE_CONDA_VERSION=$(conda --version 2>/dev/null | awk '{print $2}' || echo unknown)
        local base; base=$(conda info --base 2>/dev/null || echo "")
        if [[ -n "$base" ]]; then
            local seen=0 r
            for r in "${VEDA_PROBE_CONDA_ROOTS[@]}"; do [[ "$r" == "$base" ]] && seen=1; done
            (( seen )) || VEDA_PROBE_CONDA_ROOTS+=("$base")
        fi
    fi

    # An ACTIVE base is the thing that actually causes trouble: it is what puts
    # CONDA_PREFIX and friends into the environment we inherit.
    if [[ -n "${CONDA_PREFIX:-}" ]]; then
        VEDA_PROBE_CONDA_ACTIVE="${CONDA_DEFAULT_ENV:-$CONDA_PREFIX}"
    fi

    if (( ${#VEDA_PROBE_CONDA_ROOTS[@]} )); then
        log_info "existing conda: ${VEDA_PROBE_CONDA_ROOTS[0]}${VEDA_PROBE_CONDA_VERSION:+ (conda $VEDA_PROBE_CONDA_VERSION)}"
        log_info "VEDA will not modify it; VEDA uses its own micromamba under $VEDA_MAMBA_ROOT"
    else
        log_info "no existing conda installation found (none is needed)"
    fi

    # The user's ~/.condarc is read by micromamba too. `defaults` is Anaconda's
    # own channel: it is licence-gated for some users and recent conda/mamba
    # refuse to use it until terms are accepted, which would fail our solves
    # for a reason that has nothing to do with VEDA.
    VEDA_PROBE_CONDARC_DEFAULTS=0
    if [[ -r "$HOME/.condarc" ]] && grep -qE '^\s*-\s*defaults\s*$' "$HOME/.condarc" 2>/dev/null; then
        VEDA_PROBE_CONDARC_DEFAULTS=1
        veda::_note "~/.condarc lists the 'defaults' channel; VEDA's solves ignore your rc files"
    fi

    return 0
}

# --- python / pip ----------------------------------------------------------

veda::_probe_python() {
    VEDA_PROBE_SYS_PYTHON=""
    VEDA_PROBE_SYS_PYVER=""
    VEDA_PROBE_SYS_PIP=0
    VEDA_PROBE_PEP668=0
    VEDA_PROBE_USERSITE=()

    if have python3; then
        VEDA_PROBE_SYS_PYTHON=$(command -v python3)
        VEDA_PROBE_SYS_PYVER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo unknown)
        python3 -m pip --version >/dev/null 2>&1 && VEDA_PROBE_SYS_PIP=1

        # PEP 668: Debian/Ubuntu mark the system Python as externally managed
        # so `pip install` into it fails. Irrelevant to us -- we never install
        # into the system Python -- but worth recording, because it is the
        # usual reason a user believes "pip is broken on this machine".
        local marker
        marker=$(python3 -c 'import sysconfig,os;print(os.path.join(sysconfig.get_path("stdlib"),"EXTERNALLY-MANAGED"))' 2>/dev/null || echo "")
        [[ -n "$marker" && -e "$marker" ]] && VEDA_PROBE_PEP668=1
    fi

    # The shadowing hazard, quantified. Anything here outranks an env's own
    # packages unless PYTHONNOUSERSITE=1 is set.
    local d n
    for d in "$HOME"/.local/lib/python*/site-packages; do
        [[ -d "$d" ]] || continue
        n=$(find "$d" -maxdepth 1 -name '*.dist-info' -printf . 2>/dev/null | wc -c || echo 0)
        VEDA_PROBE_USERSITE+=("${d##*/lib/}:$n")
    done

    log_info "system python: ${VEDA_PROBE_SYS_PYVER:-absent}${VEDA_PROBE_SYS_PIP:+, pip present}$( ((VEDA_PROBE_PEP668)) && echo ", PEP 668 externally-managed" )"

    if (( ${#VEDA_PROBE_USERSITE[@]} )); then
        local e
        for e in "${VEDA_PROBE_USERSITE[@]}"; do
            log_warn "~/.local/${e%%:*} holds ${e##*:} packages -- these shadow every environment unless suppressed"
        done
        veda::_note "VEDA sets PYTHONNOUSERSITE=1 everywhere, so your ~/.local packages are ignored rather than removed"
    fi

    # We never need the system pip: every env ships its own. Say so explicitly,
    # because "pip might be running might not be" is a reasonable worry.
    (( VEDA_PROBE_SYS_PIP )) ||
        veda::_note "no working system pip; not required -- each VEDA environment installs its own"

    return 0
}

# --- hostile environment variables ----------------------------------------

veda::_probe_env_hazards() {
    VEDA_PROBE_SET_HAZARDS=()

    local v
    for v in "${VEDA_SCRUB_VARS[@]}"; do
        if [[ -n "${!v:-}" ]]; then
            VEDA_PROBE_SET_HAZARDS+=("$v")
        fi
    done

    if (( ${#VEDA_PROBE_SET_HAZARDS[@]} )); then
        log_warn "set in this shell and scrubbed by VEDA: ${VEDA_PROBE_SET_HAZARDS[*]}"
    fi

    # LD_LIBRARY_PATH gets its own treatment because it is filtered, not unset.
    VEDA_PROBE_LDLP_EMPTY=0
    VEDA_PROBE_LDLP_CUDA=0
    if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
        # An empty entry -- a leading, trailing or doubled colon -- means "the
        # current directory". The dynamic linker then searches whatever
        # directory the app happens to be started from for shared libraries.
        # VEDA extracts downloaded archives and runs from project folders, so
        # this is both a correctness and a safety problem.
        case ":${LD_LIBRARY_PATH}:" in
            *::*) VEDA_PROBE_LDLP_EMPTY=1 ;;
        esac
        [[ "$LD_LIBRARY_PATH" == *cuda* ]] && VEDA_PROBE_LDLP_CUDA=1

        (( VEDA_PROBE_LDLP_EMPTY )) &&
            veda::_hazard "LD_LIBRARY_PATH contains an empty entry, which makes the linker load shared libraries from the current directory"
        (( VEDA_PROBE_LDLP_CUDA )) &&
            veda::_note "LD_LIBRARY_PATH points at a system CUDA; it would override the CUDA libraries bundled in the PyTorch wheel, so VEDA drops it"
    fi

    # Non-UTF-8 locales break the DTU tcsh wrappers and some Perl scripts.
    VEDA_PROBE_UTF8=1
    case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
        *UTF-8*|*utf8*|*UTF8*) ;;
        "") VEDA_PROBE_UTF8=0; veda::_note "no locale set; VEDA exports C.UTF-8" ;;
        *)  VEDA_PROBE_UTF8=0; veda::_note "non-UTF-8 locale ${LC_ALL:-${LANG:-}}; VEDA exports C.UTF-8" ;;
    esac

    VEDA_PROBE_PROXY=""
    if [[ -n "${https_proxy:-${HTTPS_PROXY:-}}" ]]; then
        VEDA_PROBE_PROXY="${https_proxy:-$HTTPS_PROXY}"
        veda::_note "HTTPS proxy in use: $VEDA_PROBE_PROXY"
    fi

    return 0
}

# --- GPU -------------------------------------------------------------------

veda::_probe_gpu() {
    VEDA_PROBE_GPU_HW=0
    VEDA_PROBE_GPU_COUNT=0
    VEDA_PROBE_GPU_NAME=""
    VEDA_PROBE_GPU_DRIVER=""
    VEDA_PROBE_GPU_CUDA=""
    VEDA_PROBE_GPU_VRAM_MIB=0
    VEDA_PROBE_TORCH_FLAVOR=cpu
    VEDA_PROBE_TORCH_REASON="no NVIDIA GPU detected"

    VEDA_PROBE_GPU_HW=$( { lspci 2>/dev/null || true; } | grep -ci 'nvidia' || true )
    VEDA_PROBE_GPU_HW=${VEDA_PROBE_GPU_HW:-0}

    if [[ "${VEDA_CPU_ONLY:-0}" == 1 ]]; then
        VEDA_PROBE_TORCH_REASON="--cpu-only requested"
        log_info "GPU: ignored (--cpu-only)"
        return 0
    fi

    if ! have nvidia-smi; then
        if (( VEDA_PROBE_GPU_HW > 0 )); then
            # Hardware but no driver. We do not install drivers: that is a
            # kernel-module, reboot-requiring, distro-specific decision.
            veda::_hazard "NVIDIA hardware present but no driver (nvidia-smi missing); installing CPU PyTorch.
      VEDA does not install drivers -- that needs a kernel module and a reboot.
      To use the GPU later, install it yourself:
          sudo ubuntu-drivers install        # or: sudo apt install nvidia-driver-<version>
      then reboot and re-run:  ./install.sh --only envs
      The installer notices the CPU build no longer matches and swaps in the
      CUDA one; nothing else is rebuilt."
        fi
        log_info "GPU: none usable; PyTorch will be the CPU build"
        return 0
    fi

    if ! nvidia-smi -L >/dev/null 2>&1; then
        veda::_hazard "nvidia-smi is installed but cannot talk to the driver (kernel module not loaded, or a reboot is pending); installing CPU PyTorch"
        VEDA_PROBE_TORCH_REASON="nvidia-smi present but non-functional"
        return 0
    fi

    VEDA_PROBE_GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l || echo 0)
    VEDA_PROBE_GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo unknown)
    VEDA_PROBE_GPU_DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "")
    VEDA_PROBE_GPU_VRAM_MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0)
    VEDA_PROBE_GPU_CUDA=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | awk '{print $3}' || echo "")

    local major=${VEDA_PROBE_GPU_DRIVER%%.*}
    [[ "$major" =~ ^[0-9]+$ ]] || major=0

    # Wheel choice from the driver version.
    #
    # A CUDA runtime needs a driver at least as new as its own family minimum;
    # NVIDIA's compatibility table gives >= 525 for the CUDA 12.x family and
    # >= 450 for 11.x on Linux (exactly 525.60.13 and 450.80.02). Newer
    # drivers keep working with older runtimes -- the driver API is backward
    # compatible -- so a 13.x-era driver runs our cu121 wheels fine. That is
    # why this is a floor test and not a range test.
    if   (( major >= 525 )); then
        VEDA_PROBE_TORCH_FLAVOR=cu121
        VEDA_PROBE_TORCH_REASON="driver $VEDA_PROBE_GPU_DRIVER supports CUDA 12.x"
    elif (( major >= 450 )); then
        VEDA_PROBE_TORCH_FLAVOR=cu118
        VEDA_PROBE_TORCH_REASON="driver $VEDA_PROBE_GPU_DRIVER is too old for CUDA 12.x (needs 525+); using CUDA 11.8 wheels"
        veda::_note "$VEDA_PROBE_TORCH_REASON"
    else
        VEDA_PROBE_TORCH_FLAVOR=cpu
        VEDA_PROBE_TORCH_REASON="driver $VEDA_PROBE_GPU_DRIVER predates CUDA 11.8 (needs 450+); using CPU wheels"
        veda::_hazard "$VEDA_PROBE_TORCH_REASON. Update the driver yourself to use the GPU."
    fi

    log_ok "GPU: $VEDA_PROBE_GPU_NAME x$VEDA_PROBE_GPU_COUNT, driver $VEDA_PROBE_GPU_DRIVER, CUDA ${VEDA_PROBE_GPU_CUDA:-?}, ${VEDA_PROBE_GPU_VRAM_MIB} MiB"
    log_info "PyTorch build: $VEDA_PROBE_TORCH_FLAVOR ($VEDA_PROBE_TORCH_REASON)"

    # DeepLoc2 runs ESM-2. The 650M model in fp32 is ~2.6 GB of weights before
    # activations, so a 4 GB card manages only short sequences and small
    # batches. It is not an error -- torch falls back to CPU on OOM -- but it
    # surprises people, so say it now.
    if (( VEDA_PROBE_GPU_VRAM_MIB > 0 && VEDA_PROBE_GPU_VRAM_MIB < 6000 )); then
        veda::_note "only ${VEDA_PROBE_GPU_VRAM_MIB} MiB of VRAM; ESM-2 in DeepLoc2 may run out of memory on long proteins and fall back to CPU"
    fi
}

# --- container runtime -----------------------------------------------------

veda::_probe_container() {
    VEDA_PROBE_DOCKER=absent
    VEDA_PROBE_DOCKER_ROOTLESS=0

    if have docker; then
        if docker info >/dev/null 2>&1; then
            VEDA_PROBE_DOCKER=ok
            docker info 2>/dev/null | grep -qi 'rootless' && VEDA_PROBE_DOCKER_ROOTLESS=1
        elif sudo -n docker info >/dev/null 2>&1; then
            # Daemon is up; this user simply is not in the docker group.
            VEDA_PROBE_DOCKER=no-group
        else
            VEDA_PROBE_DOCKER=no-daemon
        fi
    elif have podman; then
        VEDA_PROBE_DOCKER=podman
        veda::_note "podman found instead of docker; install podman-docker for a 'docker' shim, or VEDA skips Vaxign-ML"
    fi

    case $VEDA_PROBE_DOCKER in
        ok)         log_ok   "docker: usable$( ((VEDA_PROBE_DOCKER_ROOTLESS)) && echo " (rootless)" )" ;;
        no-group)   log_warn "docker: daemon runs but $USER is not in the docker group (the installer can add you)" ;;
        no-daemon)  log_warn "docker: installed but the daemon is not running" ;;
        podman)     log_warn "docker: not installed; podman is present" ;;
        absent)     log_info "docker: not installed (only Vaxign-ML and local DeepTMHMM need it)" ;;
    esac

    return 0
}

# --- apt package name resolution ------------------------------------------

veda::_probe_apt() {
    VEDA_PROBE_APT_RESOLVED=()
    VEDA_PROBE_APT_OPTIONAL=()
    VEDA_PROBE_APT_MISSING=()
    VEDA_PROBE_APT_PRESENT=()

    if ! have apt-get; then
        log_info "no apt-get; system packages must be provided by hand"
        return 0
    fi

    local entry cap picked
    local -a cands
    for entry in "${VEDA_APT_CAPS[@]}"; do
        cap=${entry%%|*}
        IFS='|' read -r -a cands <<<"${entry#*|}"
        if picked=$(veda::_apt_pick "${cands[@]}"); then
            VEDA_PROBE_APT_RESOLVED+=("$picked")
            if dpkg-query -W -f='${Status}' "$picked" 2>/dev/null | grep -q 'ok installed'; then
                VEDA_PROBE_APT_PRESENT+=("$picked")
            fi
            [[ "$picked" == "${cands[0]}" ]] ||
                log_debug "apt: '$cap' resolved to $picked (not ${cands[0]}) on $VEDA_PROBE_OS_ID $VEDA_PROBE_OS_VER"
        else
            VEDA_PROBE_APT_MISSING+=("$cap (tried: ${cands[*]})")
        fi
    done

    for entry in "${VEDA_APT_CAPS_OPTIONAL[@]}"; do
        IFS='|' read -r -a cands <<<"${entry#*|}"
        picked=$(veda::_apt_pick "${cands[@]}") && VEDA_PROBE_APT_OPTIONAL+=("$picked")
    done

    log_info "apt: ${#VEDA_PROBE_APT_PRESENT[@]}/${#VEDA_PROBE_APT_RESOLVED[@]} required packages already installed"

    if (( ${#VEDA_PROBE_APT_MISSING[@]} )); then
        local m
        for m in "${VEDA_PROBE_APT_MISSING[@]}"; do
            veda::_hazard "no apt package provides '$m' on this release"
        done
    fi

    return 0
}

# --- report ----------------------------------------------------------------

veda::_probe_write_report() {
    VEDA_PROBE_REPORT="$VEDA_HOME/system-report.txt"
    mkdir -p "$VEDA_HOME" 2>/dev/null || true

    {
        printf 'VEDA system probe -- %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf 'host: %s\n\n' "$(hostname 2>/dev/null || echo unknown)"

        printf '[os]\n'
        printf '  pretty       = %s\n' "$VEDA_PROBE_OS_PRETTY"
        printf '  id/version   = %s %s (%s)\n' "$VEDA_PROBE_OS_ID" "$VEDA_PROBE_OS_VER" "$VEDA_PROBE_OS_CODENAME"
        printf '  id_like      = %s\n' "${VEDA_PROBE_OS_LIKE:-none}"
        printf '  arch/kernel  = %s / %s\n' "$VEDA_PROBE_ARCH" "$VEDA_PROBE_KERNEL"
        printf '  glibc        = %s\n' "$VEDA_PROBE_GLIBC"
        printf '  wsl          = %s\n' "$VEDA_PROBE_IS_WSL"
        printf '  container    = %s\n\n' "$VEDA_PROBE_IS_CONTAINER"

        printf '[resources]\n'
        printf '  cores        = %s\n' "$VEDA_PROBE_CORES"
        printf '  avx2         = %s\n' "$VEDA_PROBE_AVX2"
        printf '  ram_mib      = %s\n' "$VEDA_PROBE_RAM_MIB"
        printf '  swap_mib     = %s\n\n' "$VEDA_PROBE_SWAP_MIB"

        printf '[filesystem]\n'
        printf '  veda_home    = %s\n' "$VEDA_HOME"
        printf '  fstype       = %s\n' "$VEDA_PROBE_FSTYPE"
        printf '  tmp_fstype   = %s\n' "$VEDA_PROBE_TMP_FSTYPE"
        printf '  can_exec     = %s\n' "$VEDA_PROBE_EXEC_OK"
        printf '  can_symlink  = %s\n' "$VEDA_PROBE_SYMLINK_OK"
        printf '  scratch path = %s (%s/63 chars, DTU tool limit)\n\n' \
            "${VEDA_PROBE_TMPDIR:-?}" "${VEDA_PROBE_TMPDIR_LEN:-?}"

        printf '[existing conda -- not modified by VEDA]\n'
        if (( ${#VEDA_PROBE_CONDA_ROOTS[@]} )); then
            local r
            for r in "${VEDA_PROBE_CONDA_ROOTS[@]}"; do printf '  root         = %s\n' "$r"; done
        else
            printf '  root         = none found\n'
        fi
        printf '  conda ver    = %s\n' "${VEDA_PROBE_CONDA_VERSION:-n/a}"
        printf '  active env   = %s\n' "${VEDA_PROBE_CONDA_ACTIVE:-none}"
        printf '  rc defaults  = %s\n' "$VEDA_PROBE_CONDARC_DEFAULTS"
        printf '  veda mamba   = %s\n\n' "$VEDA_MAMBA_ROOT"

        printf '[python]\n'
        printf '  system py    = %s (%s)\n' "${VEDA_PROBE_SYS_PYTHON:-absent}" "${VEDA_PROBE_SYS_PYVER:-n/a}"
        printf '  system pip   = %s\n' "$VEDA_PROBE_SYS_PIP"
        printf '  pep668       = %s\n' "$VEDA_PROBE_PEP668"
        if (( ${#VEDA_PROBE_USERSITE[@]} )); then
            local e
            for e in "${VEDA_PROBE_USERSITE[@]}"; do
                printf '  user site    = ~/.local/%s -> %s packages (suppressed)\n' "${e%%:*}" "${e##*:}"
            done
        else
            printf '  user site    = empty\n'
        fi
        printf '\n'

        printf '[environment scrubbed from workers]\n'
        if (( ${#VEDA_PROBE_SET_HAZARDS[@]} )); then
            local v
            for v in "${VEDA_PROBE_SET_HAZARDS[@]}"; do printf '  %s\n' "$v"; done
        else
            printf '  nothing hostile set\n'
        fi
        printf '  ld_empty_entry = %s\n' "$VEDA_PROBE_LDLP_EMPTY"
        printf '  ld_system_cuda = %s\n\n' "$VEDA_PROBE_LDLP_CUDA"

        printf '[gpu]\n'
        printf '  pci devices  = %s\n' "$VEDA_PROBE_GPU_HW"
        printf '  usable gpus  = %s\n' "$VEDA_PROBE_GPU_COUNT"
        printf '  name         = %s\n' "${VEDA_PROBE_GPU_NAME:-n/a}"
        printf '  driver       = %s\n' "${VEDA_PROBE_GPU_DRIVER:-n/a}"
        printf '  cuda (smi)   = %s\n' "${VEDA_PROBE_GPU_CUDA:-n/a}"
        printf '  vram_mib     = %s\n' "$VEDA_PROBE_GPU_VRAM_MIB"
        printf '  torch build  = %s\n' "$VEDA_PROBE_TORCH_FLAVOR"
        printf '  reason       = %s\n\n' "$VEDA_PROBE_TORCH_REASON"

        printf '[docker]\n'
        printf '  status       = %s\n' "$VEDA_PROBE_DOCKER"
        printf '  rootless     = %s\n\n' "$VEDA_PROBE_DOCKER_ROOTLESS"

        printf '[apt packages resolved for this release]\n'
        if (( ${#VEDA_PROBE_APT_RESOLVED[@]} )); then
            printf '  required     = %s\n' "${VEDA_PROBE_APT_RESOLVED[*]}"
        fi
        if (( ${#VEDA_PROBE_APT_OPTIONAL[@]} )); then
            printf '  optional     = %s\n' "${VEDA_PROBE_APT_OPTIONAL[*]}"
        fi
        printf '  already here = %s/%s\n' "${#VEDA_PROBE_APT_PRESENT[@]}" "${#VEDA_PROBE_APT_RESOLVED[@]}"
        if (( ${#VEDA_PROBE_APT_MISSING[@]} )); then
            local m
            for m in "${VEDA_PROBE_APT_MISSING[@]}"; do printf '  UNAVAILABLE  = %s\n' "$m"; done
        fi
        printf '\n'

        printf '[hazards]\n'
        if (( ${#VEDA_PROBE_HAZARDS[@]} )); then
            local h
            for h in "${VEDA_PROBE_HAZARDS[@]}"; do printf '  - %s\n' "$h"; done
        else
            printf '  none\n'
        fi
        printf '\n[notes]\n'
        if (( ${#VEDA_PROBE_NOTES[@]} )); then
            local n
            for n in "${VEDA_PROBE_NOTES[@]}"; do printf '  - %s\n' "$n"; done
        else
            printf '  none\n'
        fi
    } >"$VEDA_PROBE_REPORT" 2>/dev/null || {
        log_warn "could not write the probe report to $VEDA_PROBE_REPORT"
        return 0
    }

    log_ok "system report written to $VEDA_PROBE_REPORT"
}

veda::_probe_summarise() {
    if (( ${#VEDA_PROBE_NOTES[@]} )); then
        local n
        for n in "${VEDA_PROBE_NOTES[@]}"; do log_info "note: $n"; done
    fi

    (( ${#VEDA_PROBE_HAZARDS[@]} )) || { log_ok "no blocking problems found"; return 0; }

    local h
    for h in "${VEDA_PROBE_HAZARDS[@]}"; do log_warn "$h"; done

    # Only the two filesystem failures and a missing required package make the
    # install pointless. Everything else degrades: CPU instead of GPU, no
    # Docker tools, a slow BLAS. So warn and continue unless it is fatal.
    local fatal=0
    (( VEDA_PROBE_EXEC_OK )) || fatal=1
    (( VEDA_PROBE_SYMLINK_OK )) || fatal=1
    (( ${#VEDA_PROBE_APT_MISSING[@]} )) && fatal=1
    # A too-long VEDA_HOME is fatal rather than a warning: the install would
    # otherwise finish "successfully" with the three DTU tools core-dumping,
    # and it costs nothing to fix before anything is downloaded.
    (( ${VEDA_PROBE_TMPDIR_LEN:-0} > 63 )) && fatal=1

    if (( fatal )); then
        die "the probe found problems that will stop the install.
        See $VEDA_PROBE_REPORT for the full picture.
        Fix the items above, then re-run: ./install.sh"
    fi

    log_warn "continuing: the items above reduce functionality but do not block the install"

    # `|| true` is required: pause_for returns 1 when there is no terminal, and
    # this is the last command in the function.
    pause_for "press Enter to continue, or Ctrl-C to stop and fix them first" || true
    return 0
}
