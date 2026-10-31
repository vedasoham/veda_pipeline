#!/usr/bin/env bash
# VEDA installer :: stage 4 -- the four conda environments.
#
#   veda-ui      Streamlit app + dashboards + DeepLoc2            (py3.10)
#   veda-tools   AlgPred2/ToxinPred2/IL2Pred/IL4Pred2 + BLAST     (py3.9)
#   veda-tmhmm   DeepTMHMM via pybiolib                           (py3.9)
#   veda-ifn     IFNEpitope2                                      (py3.10)
#
# Each env is created from installer/envs/<name>.yml. PyTorch is installed
# afterwards, from the wheel index that matches this machine's GPU, because a
# YAML file cannot express that choice.

VEDA_ENVS=(veda-ui veda-tools veda-tmhmm veda-ifn)

# Torch build verified with this pipeline. Which wheel index is used is NOT
# decided here -- the probe decides it from the actual driver version and
# publishes the answer as VEDA_PROBE_TORCH_FLAVOR. See veda::_probe_gpu.
VEDA_TORCH_VERSION="2.5.1"

veda::_torch_index() {
    case "${VEDA_PROBE_TORCH_FLAVOR:-cpu}" in
        cu121) printf 'https://download.pytorch.org/whl/cu121' ;;
        cu118) printf 'https://download.pytorch.org/whl/cu118' ;;
        *)     printf 'https://download.pytorch.org/whl/cpu'   ;;
    esac
}

veda::envs_create() {
    # Works when this stage runs on its own (./install.sh --only envs), and
    # re-reads the GPU each time, so installing a driver and re-running is
    # enough to move from CPU to CUDA wheels.
    veda::probe_ensure

    stage_header "Python environments"

    local name yml
    for name in "${VEDA_ENVS[@]}"; do
        yml="$VEDA_INSTALLER_DIR/envs/$name.yml"
        [[ -f "$yml" ]] || die "environment spec missing: $yml"

        if env_exists "$name" && [[ "${VEDA_FORCE:-0}" != 1 ]]; then
            log_skip "environment $name"
            continue
        fi

        if env_exists "$name"; then
            log_info "re-creating $name (--force)"
            run "remove env $name" mamba env remove -y -n "$name" || true
        elif [[ -d "$(env_prefix "$name")" ]]; then
            # A prefix with no interpreter means a previous create was
            # interrupted or failed mid-transaction. mamba will not write into
            # it, so clear it rather than failing on every later run.
            log_warn "$name exists but has no interpreter; clearing the partial environment"
            rm -rf "$(env_prefix "$name")"
        fi

        log_info "creating $name from ${yml##*/} (this takes a few minutes)"
        if ! retry 2 "create env $name" mamba create -y -n "$name" -f "$yml"; then
            die "could not create the '$name' environment.
        The full solver output is in:
            $VEDA_LOG
        Most common cause is a transient channel error -- re-running
        ./install.sh usually clears it."
        fi
        env_exists "$name" || die "env '$name' was created but has no python; aborting"
        log_ok "environment $name ready ($("$(env_python "$name")" -V 2>&1))"
    done

    # Repair environments that already exist. A version bound in a yml only
    # applies when the environment is CREATED, so an env built before the
    # bound was added keeps whatever it got. Re-running the installer must fix
    # it rather than require --force and a full rebuild.
    for name in "${VEDA_ENVS[@]}"; do
        veda::_envs_fix_setuptools "$name"
    done

    # TORCH FIRST. This ordering is load-bearing, not tidiness.
    #
    # veda-ui.pip.txt contains pytorch-lightning, which lists `torch` as a
    # dependency with no version bound. If pip runs first it satisfies that
    # from PyPI's default index, which serves whatever the newest torch is,
    # built against whatever CUDA that release happens to target. That wheel:
    #
    #   * ignores the driver-aware index this installer chose (so a machine
    #     with a CUDA 12-era driver gets CUDA 13 wheels that cannot run), and
    #   * then makes the explicit torch install below skip, because torch is
    #     already "present".
    #
    # Installing the pinned, probe-selected build first means pip sees the
    # requirement already satisfied and leaves it alone.
    veda::_envs_install_torch

    # PyPI dependencies, always (also repairs an env created before this step
    # existed). Deliberately NOT a `pip:` section in the yml -- see
    # veda::_envs_pip for why that silently installs nothing.
    for name in "${VEDA_ENVS[@]}"; do
        veda::_envs_pip "$name"
    done

    # Re-check setuptools AFTER pip, not only before it.
    #
    # The conda `setuptools<81` bound constrains conda's solve; it does not
    # constrain pip. Any wheel declaring `setuptools>=<something>` can make pip
    # upgrade it past 81 inside the environment, which removes pkg_resources
    # again and breaks DeepLoc2 and ifnepitope2 -- after the earlier repair has
    # already run and decided there was nothing to do.
    #
    # To be clear about the evidence: this has NOT been observed with the
    # current veda-ui.pip.txt. A `pip install --dry-run -r` of it leaves
    # setuptools alone, and no package in it declares a setuptools dependency.
    # The guard is here because the pin lives in a file people will add to, the
    # failure it prevents is silent and only surfaces at prediction time, and
    # repeating the check is free -- it is a no-op when setuptools is already
    # correct, and it runs before DeepLoc2 is installed and before the import
    # checks, so a regression cannot reach the user.
    for name in "${VEDA_ENVS[@]}"; do
        veda::_envs_fix_setuptools "$name"
    done

    veda::_envs_install_deeploc
    veda::_envs_sanity
}

# --- PyPI dependencies ----------------------------------------------------
#
# Every pip invocation in this installer runs with PYTHONNOUSERSITE=1. This is
# not optional hygiene, it is load-bearing:
#
#   ~/.local/lib/pythonX.Y/site-packages is visible to a conda env AND sits
#   BEFORE the env's own site-packages on sys.path. If the user has ever run
#   `pip install --user`, pip reports those packages "already satisfied" and
#   installs nothing into the env. The env ends up empty while every import
#   still appears to work -- until the app runs somewhere else, or the user
#   cleans ~/.local, and then nothing works for no visible reason.
#
# This is also why a `pip:` block inside the env yml is not used: micromamba
# shells out to pip without the variable set, and the same thing happens.

# Every pip install in this installer goes through here. Never call
# `$py -m pip install` directly -- one unguarded call silently undoes the
# isolation for whatever it installs, and the failure is invisible: pip
# reports "already satisfied" and exits 0.
#
# What each variable defends against, on an arbitrary user's machine:
#
#   PYTHONNOUSERSITE=1         ~/.local/lib/pythonX.Y/site-packages is on the
#                              env's sys.path AHEAD of its own site-packages.
#                              Without this, pip sees the user's --user
#                              packages, calls the requirement satisfied and
#                              installs nothing into the environment.
#   PIP_USER=0                 `PIP_USER=1` in the environment, or `user=true`
#                              in ~/.config/pip/pip.conf, redirects the install
#                              to ~/.local even when pip is the env's own pip.
#   PIP_REQUIRE_VIRTUALENV=0   `require-virtualenv=true` in pip.conf makes pip
#                              refuse to run at all outside a venv; a conda
#                              prefix does not count as one.
#   PIP_TARGET=                a pip.conf `target=` would divert every install
#                              to that directory.
#   PIP_PREFIX=                same hazard via `prefix=`.
veda::_pip() {
    local py=$1; shift
    env PYTHONNOUSERSITE=1 \
        PIP_USER=0 \
        PIP_REQUIRE_VIRTUALENV=0 \
        PIP_TARGET= \
        PIP_PREFIX= \
        PIP_DISABLE_PIP_VERSION_CHECK=1 \
        "$py" -m pip install --no-input "$@"
}

# DeepLoc2 and ifnepitope2 both `import pkg_resources`, which setuptools
# stopped bundling after 80.x. The yml files pin setuptools<81, but that only
# binds at creation time, so check and repair in place.
#
# Detected by importing rather than by comparing version numbers: the question
# that matters is whether the module is there, and that keeps working if
# setuptools ever brings it back or a distro patches it out early.
# Is this env's setuptools both old enough and complete?
#
# Both halves are checked. "pkg_resources imports" alone is not enough: an env
# can end up with setuptools 84 reporting its own version while pkg_resources
# is left behind on disk from an earlier 80.x. That mixed state imports fine
# but emits setuptools' deprecation warning on stderr on every tool call, and
# is one `pip install` away from breaking for real.
veda::_envs_setuptools_ok() {
    local py=$1
    "$py" -s -c 'import pkg_resources' 2>/dev/null || return 1
    "$py" -s -c '
import setuptools, sys
try:
    major = int(setuptools.__version__.split(".")[0])
except Exception:
    sys.exit(1)
sys.exit(0 if major < 81 else 1)
' 2>/dev/null
}

veda::_envs_fix_setuptools() {
    local name=$1 py
    py=$(env_python "$name")
    [[ -x "$py" ]] || return 0

    veda::_envs_setuptools_ok "$py" && return 0

    local sv
    sv=$("$py" -s -c 'import setuptools;print(setuptools.__version__)' 2>/dev/null || echo unknown)
    log_warn "$name: setuptools $sv does not cleanly provide pkg_resources (DeepLoc2 and ifnepitope2 need it)"

    # conda first: setuptools is a conda dependency in every env spec.
    run "pin setuptools<81 in $name" mamba install -y -n "$name" 'setuptools<81' || true

    # Then pip, because a pip-installed setuptools shadows the conda one and
    # keeps reporting its own version no matter what conda does. Only reached
    # when conda alone did not settle it.
    if ! veda::_envs_setuptools_ok "$py"; then
        log_info "$name: a pip-installed setuptools is shadowing the conda one; pinning with pip"
        run "pip pin setuptools<81 in $name" veda::_pip "$py" 'setuptools<81' || true
    fi

    if veda::_envs_setuptools_ok "$py"; then
        log_ok "$name: setuptools $("$py" -s -c 'import setuptools;print(setuptools.__version__)' 2>/dev/null), pkg_resources present"
    else
        log_warn "$name: could not get a setuptools<81 with pkg_resources"
        log_warn "  DeepLoc2 and ifnepitope2 may fail; try: ./install.sh --from envs --force"
    fi
    return 0
}

veda::_envs_pip() {
    local name=$1
    local req="$VEDA_INSTALLER_DIR/envs/$name.pip.txt"
    [[ -f "$req" ]] || { log_debug "$name has no pip requirements"; return 0; }

    local py; py=$(env_python "$name")
    [[ -x "$py" ]] || { log_warn "$name has no interpreter; skipping pip step"; return 0; }

    log_info "installing PyPI dependencies for $name"
    if ! retry 2 "pip install -r ${name}.pip.txt" \
            veda::_pip "$py" -r "$req"; then
        die "could not install the PyPI dependencies for '$name'.
        See $VEDA_LOG, then re-run:
            ./install.sh --from envs"
    fi
    log_ok "$name PyPI dependencies installed"
}

# --- PyTorch --------------------------------------------------------------

veda::_envs_install_torch() {
    local py; py=$(env_python veda-ui)

    local index flavor want
    flavor=${VEDA_PROBE_TORCH_FLAVOR:-cpu}
    index=$(veda::_torch_index)

    # What torch.version.cuda reports for the build this machine should have.
    case $flavor in
        cu121) want=12.1 ;;
        cu118) want=11.8 ;;
        *)     want=cpu  ;;
    esac

    # -s throughout: a `pip install --user torch` in ~/.local would otherwise
    # make this look installed, and DeepLoc would then break on any machine
    # that lacks it.
    if "$py" -s -c 'import torch' 2>/dev/null && [[ "${VEDA_FORCE:-0}" != 1 ]]; then
        local v have
        v=$("$py" -s -c 'import torch;print(torch.__version__)' 2>/dev/null || echo unknown)
        have=$("$py" -s -c 'import torch;print(torch.version.cuda or "cpu")' 2>/dev/null || echo unknown)

        # Reinstall when the installed build no longer matches the machine.
        # This is what makes "install the NVIDIA driver, then re-run the
        # installer" work without --force, and it also self-corrects an
        # install cloned from a GPU machine onto a CPU-only one, or a driver
        # downgrade that leaves CUDA 12 wheels on a CUDA 11 driver.
        if [[ "$have" == "$want" ]]; then
            log_skip "PyTorch in veda-ui (${v}, ${flavor})"
            return 0
        fi
        log_info "installed PyTorch is a '${have}' build but this machine wants '${want}' (${flavor}); reinstalling"
    fi

    log_info "installing torch ${VEDA_TORCH_VERSION} (${flavor}) -- ${VEDA_PROBE_TORCH_REASON:-no GPU probe ran}"
    [[ "$flavor" == cpu ]] && log_info "(DeepLoc2 and DeepTMHMM still work on CPU, just slower)"
    log_info "fetching from ${index##*/} wheels; this is a large download"
    if ! retry 2 "pip install torch" veda::_pip "$py" \
            --index-url "$index" "torch==${VEDA_TORCH_VERSION}"; then
        log_warn "pinned torch ${VEDA_TORCH_VERSION} unavailable for this Python/platform"
        log_info "falling back to the newest torch on ${index##*/}"
        retry 2 "pip install torch (unpinned)" veda::_pip "$py" \
            --index-url "$index" torch \
            || die "could not install PyTorch.
        DeepLoc2 needs it. Retry, or install by hand:
            PYTHONNOUSERSITE=1 $py -m pip install --index-url $index torch"
    fi

    local tv; tv=$("$py" -s -c 'import torch;print(torch.__version__)' 2>/dev/null || echo unknown)
    log_ok "PyTorch $tv installed in veda-ui"
    if [[ "$flavor" == cu* ]]; then
        # Run with a scrubbed LD_LIBRARY_PATH. A system CUDA on that path takes
        # precedence over the CUDA libraries bundled inside the wheel, which is
        # a common cause of "installed fine, cannot see the GPU".
        if env -u LD_LIBRARY_PATH "$py" -s \
                -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null; then
            log_ok "CUDA is visible to PyTorch"
        elif "$py" -s -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null; then
            log_ok "CUDA is visible to PyTorch (only with LD_LIBRARY_PATH set)"
        else
            log_warn "${flavor} wheels installed but torch.cuda.is_available() is False"
            log_warn "usually a driver mismatch; DeepLoc2 will fall back to CPU"
            log_warn "driver seen by the probe: ${VEDA_PROBE_GPU_DRIVER:-none}"
        fi
    fi
}

# --- DeepLoc2 -------------------------------------------------------------
# DeepLoc 2.1 is not on PyPI; it is installed from the licence-gated bundle in
# resources/tools/deeploc-2.1.All. The worker invokes a bare `deeploc2`, so the
# console script has to land in veda-ui/bin (the launcher puts that on PATH).

veda::_envs_install_deeploc() {
    local py pkg
    py=$(env_python veda-ui)
    pkg="$VEDA_TOOLS_DIR/deeploc-2.1.All/deeploc2_package"

    if [[ -x "$(env_bin veda-ui deeploc2)" ]] && [[ "${VEDA_FORCE:-0}" != 1 ]]; then
        log_skip "DeepLoc2 in veda-ui"
        return 0
    fi

    if [[ ! -f "$pkg/setup.py" ]]; then
        log_warn "DeepLoc2 package not found at $pkg"
        log_warn "subcellular localisation will be unavailable until it is installed"
        log_warn "see the DTU link printed by the tools stage"
        return 0
    fi

    strip_mac_cruft "$VEDA_TOOLS_DIR/deeploc-2.1.All"

    log_info "installing DeepLoc2 from the bundled package"
    # --no-deps: the yml already pins the dependency set. Letting DeepLoc's
    # own setup.py resolve them re-solves torch and can pull numpy 2.x.
    if ! run "pip install DeepLoc2" veda::_pip "$py" --no-deps "$pkg"; then
        log_warn "DeepLoc2 install failed; continuing without it (see $VEDA_LOG)"
        return 0
    fi

    if [[ -x "$(env_bin veda-ui deeploc2)" ]]; then
        log_ok "DeepLoc2 installed ($(env_bin veda-ui deeploc2))"
    else
        log_warn "DeepLoc2 installed but no deeploc2 console script appeared"
        log_warn "the worker calls 'deeploc2' by name and will fail"
    fi
}

# --- verification ---------------------------------------------------------

veda::_envs_sanity() {
    log_info "import-checking each environment"
    local failed=0

    # Keep these lists in step with installer/envs/*.yml AND with what the
    # tools actually import -- see the header of each yml. veda-tmhmm is
    # deliberately minimal: tmhmm_worker.py and tmhmm_runner.py import only
    # biolib and typing_extensions, nothing from the numeric stack.
    # pkg_resources is in the veda-ui list because DeepLoc2 imports it. It is
    # supplied by setuptools<81; see installer/envs/veda-ui.yml.
    veda::_env_check veda-ui    "streamlit pandas numpy plotly Bio openpyxl matplotlib_venn biolib pkg_resources torch" || failed=1
    veda::_env_check veda-tools "pandas numpy sklearn joblib"                                       || failed=1
    veda::_env_check veda-tmhmm "biolib typing_extensions"                                          || failed=1
    veda::_env_check veda-ifn   "pandas numpy sklearn tqdm pkg_resources"                           || failed=1

    # BLAST must be inside veda-tools/bin: the workers derive its path from
    # config.TOOL_PYTHON's parent directory, not from $PATH.
    local blastp; blastp=$(env_bin veda-tools blastp)
    if [[ -x "$blastp" ]]; then
        log_ok "blastp present in veda-tools ($("$blastp" -version 2>/dev/null | head -1))"
    else
        log_err "blastp missing from veda-tools/bin"
        log_err "AlgPred2, ToxinPred2 and IFNEpitope2 all need it there"
        failed=1
    fi

    if (( failed )); then
        die "environment verification failed. See $VEDA_LOG, then re-run:
            ./install.sh --from envs --force"
    fi
    log_ok "all environments import cleanly"
}

veda::_env_check() {
    local name=$1 mods=$2 py m bad=()
    py=$(env_python "$name")
    for m in $mods; do
        # -s: ignore ~/.local, or this validates the wrong packages entirely.
        "$py" -s -c "import $m" 2>>"$VEDA_LOG" || bad+=("$m")
    done
    if (( ${#bad[@]} > 0 )); then
        log_err "$name: failed to import: ${bad[*]}"
        return 1
    fi
    log_ok "$name imports OK"
    return 0
}

# --- optional model pre-warm ---------------------------------------------
# DeepLoc2 downloads ESM-1b (~7 GB of weights, ~2.5 GB on disk after caching)
# from HuggingFace on its first run. Doing it here means the first real
# analysis is not a surprise 20-minute wait.

veda::envs_prewarm_models() {
    stage_header "Pre-downloading DeepLoc model weights"

    local py; py=$(env_python veda-ui)
    if ! "$py" -s -c 'import esm' 2>/dev/null; then
        log_warn "fair-esm not importable in veda-ui; skipping pre-warm"
        return 0
    fi

    log_info "fetching ESM-1b weights into $VEDA_HOME/cache (several GB, one time)"
    log_info "safe to interrupt -- DeepLoc will simply download them on first use"

    TORCH_HOME="$VEDA_HOME/cache/torch" \
    HF_HOME="$VEDA_HOME/cache/huggingface" \
    run "download ESM weights" "$py" -I -c '
from esm import pretrained
pretrained.load_model_and_alphabet("esm1b_t33_650M_UR50S")
print("esm1b cached")
' || { log_warn "model pre-warm did not complete; DeepLoc will fetch on demand"; return 0; }

    log_ok "DeepLoc model weights cached"
}
