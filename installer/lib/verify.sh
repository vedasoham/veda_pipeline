#!/usr/bin/env bash
# VEDA installer :: stage 9 -- prove the install actually works.
#
# A green install that cannot run a prediction is worthless, so this stage
# executes every tool against a tiny real input and checks the output. It is
# also the body of ./veda-doctor, which can be re-run at any time.
#
# Each check reports PASS / FAIL / SKIP:
#   SKIP  the tool is not installed (licence-gated, or Docker absent).
#         Not an error -- the matching pipeline step is simply unavailable.
#   FAIL  the tool is installed but does not run. This is a real problem.
#
# Exit status: non-zero only when a CORE check fails, or when any installed
# tool fails. Missing optional tools never fail the doctor.

VEDA_CHK_PASS=0
VEDA_CHK_FAIL=0
VEDA_CHK_SKIP=0
declare -ga VEDA_CHK_REPORT=()

veda::verify() {
    stage_header "Verification"

    VEDA_VERIFY_WORK="$VEDA_HOME/tmp/verify-$$"
    mkdir -p "$VEDA_VERIFY_WORK"
    trap 'rm -rf "${VEDA_VERIFY_WORK:-}"' RETURN

    veda::_mk_fixtures

    # --- core: nothing works without these -------------------------------
    veda::_chk core "micromamba"        veda::_t_mamba
    veda::_chk core "veda-ui python"    veda::_t_py veda-ui
    veda::_chk core "veda-tools python" veda::_t_py veda-tools
    veda::_chk core "veda-tmhmm python" veda::_t_py veda-tmhmm
    veda::_chk core "veda-ifn python"   veda::_t_py veda-ifn
    veda::_chk core "config.py loads"   veda::_t_config
    veda::_chk core "streamlit"         veda::_t_streamlit
    veda::_chk core "veda_paths.json"   veda::_t_paths_json
    veda::_chk core "env isolation"     veda::_t_isolation
    veda::_chk core "BLAST (blastp)"    veda::_t_blastp
    veda::_chk core "tcsh interpreter"  veda::_t_tcsh
    veda::_chk core "perl interpreter"  veda::_t_perl

    # --- prediction tools -------------------------------------------------
    veda::_chk tool "NetMHCpan 4.2"     veda::_t_netmhcpan
    veda::_chk tool "NetMHCIIpan 4.3"   veda::_t_netmhciipan
    veda::_chk tool "GibbsCluster 2.0"  veda::_t_gibbs
    veda::_chk tool "Seq2Logo 2.2"      veda::_t_seq2logo
    veda::_chk tool "AlgPred 2.0"       veda::_t_algpred
    veda::_chk tool "ToxinPred 2.0"     veda::_t_toxinpred
    veda::_chk tool "IL2Pred"           veda::_t_il2pred
    veda::_chk tool "IL4Pred 2.0"       veda::_t_il4pred
    veda::_chk tool "IFNEpitope 2.0"    veda::_t_ifnepitope
    veda::_chk tool "DeepLoc 2.1"       veda::_t_deeploc
    veda::_chk tool "DeepTMHMM (biolib)" veda::_t_biolib
    veda::_chk tool "Vaxign-ML image"   veda::_t_vaxign_image
    veda::_chk tool "DeepTMHMM image"   veda::_t_tmhmm_image
    veda::_chk tool "launcher"          veda::_t_launcher

    veda::_verify_report
}

# --- harness ----------------------------------------------------------------

# _chk <class> <label> <fn...>   class is "core" or "tool"
veda::_chk() {
    local class=$1 label=$2; shift 2
    local out rc
    # The `&& ... || ...` form is load-bearing: a bare `out=$(...)` would be a
    # failing simple command under `set -e`, firing the ERR trap and aborting
    # the whole run on the first failed or skipped check. Wrapping it in an
    # || list also suppresses `set -e` inside the called check function, so
    # the individual tests can use plain pipelines without guarding each one.
    out=$("$@" 2>&1) && rc=0 || rc=$?

    case $rc in
        0)
            VEDA_CHK_PASS=$(( VEDA_CHK_PASS + 1 ))
            printf '   %s✔%s %-22s %s%s%s\n' "$C_GREEN" "$C_RESET" "$label" "$C_DIM" "${out:-ok}" "$C_RESET"
            VEDA_CHK_REPORT+=("PASS|$class|$label|$out")
            veda::_log_raw "CHECK PASS $label :: $out"
            ;;
        77)
            VEDA_CHK_SKIP=$(( VEDA_CHK_SKIP + 1 ))
            printf '   %s○%s %-22s %s%s%s\n' "$C_YELLOW" "$C_RESET" "$label" "$C_DIM" "${out:-not installed}" "$C_RESET"
            VEDA_CHK_REPORT+=("SKIP|$class|$label|$out")
            veda::_log_raw "CHECK SKIP $label :: $out"
            ;;
        *)
            VEDA_CHK_FAIL=$(( VEDA_CHK_FAIL + 1 ))
            printf '   %s✘%s %-22s %s\n' "$C_RED" "$C_RESET" "$label" "${out:-failed}"
            VEDA_CHK_REPORT+=("FAIL|$class|$label|$out")
            veda::_log_raw "CHECK FAIL $label :: $out"
            ;;
    esac
    return 0
}

skip() { printf '%s' "$1"; return 77; }

# --- fixtures ---------------------------------------------------------------

veda::_mk_fixtures() {
    local w=$VEDA_VERIFY_WORK
    # 9-mers: valid input for MHC-I, AlgPred, ToxinPred, IL2Pred.
    printf 'AAAWYLWEV\nSLYNTVATL\nGILGFVFTL\n' >"$w/pep9.txt"
    # Short FASTA for the peptide-level predictors.
    printf '>pep1\nAAAWYLWEV\n>pep2\nSLYNTVATL\n>pep3\nGILGFVFTL\n' >"$w/pep.fa"
    # 15-mers for MHC-II.
    printf '>mhc2_1\nPKYVKQNTLKLATGM\n>mhc2_2\nAGFKGEQGPKGEPGP\n' >"$w/pep15.fa"
    # A short protein for DeepLoc / DeepTMHMM.
    printf '>sp_test\nMKTIIALSYIFCLVFAQKLPGNDNSTATLCLGHHAVPNGTLVKTITDDQIEVTNATELVQ\n' >"$w/prot.fa"
    log_debug "fixtures in $w"
}

# --- core checks ------------------------------------------------------------

veda::_t_mamba() {
    [[ -x "$VEDA_MAMBA_BIN" ]] || { echo "micromamba missing at $VEDA_MAMBA_BIN"; return 1; }
    "$VEDA_MAMBA_BIN" --version 2>/dev/null | head -1
}

veda::_t_py() {
    local name=$1 py; py=$(env_python "$name")
    [[ -x "$py" ]] || { echo "no interpreter at $py"; return 1; }
    "$py" -V 2>&1 | head -1
}

veda::_t_config() {
    local py; py=$(env_python veda-ui)
    [[ -x "$py" ]] || { echo "veda-ui missing"; return 1; }
    # -I so the tools' stray json.py / typing.py cannot shadow stdlib imports.
    # No f-strings in any Python embedded in this installer.
    #
    # Nesting the same quote character inside an f-string -- f"{x.split("/")}"
    # -- is only legal from Python 3.12 (PEP 701). These snippets run under the
    # environments' own interpreters, which are 3.9 and 3.10, so such an
    # f-string is a SyntaxError there. %-formatting works on every version and
    # cannot produce this class of failure, which is doubly worth avoiding
    # because the error surfaces as "config.py loads: SyntaxError" and points
    # at the wrong file.
    ( cd "$VEDA_APP_DIR/src" && "$py" -I -c '
import sys
sys.path.insert(0, ".")
import config
n = len(getattr(config, "NETMHCPAN_ALLELES", []))
tp = config.TOOL_PYTHON
label = tp.split("/")[-3] if "/" in tp else tp
print("ok, %d MHC-I alleles, TOOL_PYTHON=%s" % (n, label))
' 2>&1 | tail -1 )
}

veda::_t_streamlit() {
    local b; b=$(env_bin veda-ui streamlit)
    [[ -x "$b" ]] || { echo "streamlit not in veda-ui/bin"; return 1; }
    "$b" version 2>/dev/null | head -1 || { echo "streamlit will not run"; return 1; }
}

veda::_t_paths_json() {
    local f="$VEDA_APP_DIR/veda_paths.json"
    [[ -f "$f" ]] || { echo "missing; run ./install.sh --only configure"; return 1; }
    local py; py=$(env_python veda-ui)
    "$py" -I -c "
import json,sys
d=json.load(open('$f'))
miss=[k for k,v in d['python'].items() if not v]
print('schema %s, %d interpreters' % (d['schema'], len(d['python'])) if not miss else 'incomplete: %s' % miss)
sys.exit(1 if miss else 0)
" 2>&1
}

# A conda env still reads ~/.local/lib/pythonX.Y/site-packages, and it sits
# BEFORE the env's own site-packages on sys.path. Anything pip-installed with
# --user therefore overrides every version VEDA pins, which is exactly the
# kind of fault that shows up as an inexplicable result rather than an error.
# The launcher sets PYTHONNOUSERSITE=1; this check proves the env would still
# be correct without it, and names what leaks if not.
# Does the isolation WORK -- not "could ~/.local shadow the env".
#
# The first version of this check probed with PYTHONNOUSERSITE unset and failed
# whenever ~/.local held any of these packages. That is a false alarm: ~/.local
# having packages is the normal state on a researcher's machine, and it is not
# a defect as long as VEDA suppresses them. It reported a failure on a
# correctly isolated install, which trains people to ignore the check.
#
# What actually matters, and what this tests:
#   1. with the guard the launcher sets, nothing resolves out of ~/.local, and
#   2. the launcher really does set it.
# The "would shadow" list is kept, but as context on a PASS.
veda::_t_isolation() {
    local py; py=$(env_python veda-ui)
    [[ -x "$py" ]] || { echo "veda-ui missing"; return 1; }

    local probe='
import sys
leaks = []
for m in ("numpy", "pandas", "sklearn", "scipy", "streamlit", "torch"):
    try:
        mod = __import__(m)
    except Exception:
        continue
    f = getattr(mod, "__file__", "") or ""
    if "/.local/" in f:
        leaks.append(m)
print(",".join(leaks))
'
    local guarded unguarded
    guarded=$(PYTHONNOUSERSITE=1 "$py" -c "$probe" 2>/dev/null) || {
        echo "could not probe sys.path"; return 1; }
    unguarded=$(PYTHONNOUSERSITE= "$py" -c "$probe" 2>/dev/null) || unguarded=''

    # Real failure: the guard is set and ~/.local still wins.
    if [[ -n "$guarded" ]]; then
        echo "BROKEN: ~/.local wins even with PYTHONNOUSERSITE=1: $guarded"
        return 1
    fi

    # Real failure: the launcher forgot the guard.
    local cmd=${VEDA_CMD:-veda} launcher
    launcher="$VEDA_HOME/bin/$cmd"
    if [[ -f "$launcher" ]] && ! grep -q 'PYTHONNOUSERSITE=1' "$launcher"; then
        echo "launcher does not set PYTHONNOUSERSITE=1"
        return 1
    fi

    if [[ -n "$unguarded" ]]; then
        echo "env wins; ~/.local would otherwise shadow ${unguarded} (suppressed by the launcher)"
    else
        echo "env takes precedence over ~/.local"
    fi
}

veda::_t_blastp() {
    local b; b=$(env_bin veda-tools blastp)
    [[ -x "$b" ]] || { echo "blastp not in veda-tools/bin (workers look for it there)"; return 1; }
    "$b" -version 2>/dev/null | head -1
}

veda::_t_tcsh() {
    have tcsh || { echo "tcsh missing; NetMHCpan/NetMHCIIpan/GibbsCluster cannot run"; return 1; }
    echo "$(command -v tcsh)"
}

veda::_t_perl() {
    have perl || { echo "perl missing; MERCI motif search cannot run"; return 1; }
    perl -e 'print "perl $]"' 2>/dev/null
}

# --- DTU tools --------------------------------------------------------------

# Shared failure triage for the two NetMHC tools.
#
# Both exit 0 on failure, and both echo the requested allele back inside their
# own parameter banner. A check that greps the whole output for the allele
# therefore PASSES on a tool that produced nothing at all -- which is exactly
# what happened: netMHCpan was reported as "predicted 3 peptide-allele rows"
# while its data directory was missing and it had produced no predictions.
#
# So: match only real result rows, which begin with a position number followed
# by the allele, and name the two failure modes explicitly.
veda::_netmhc_diagnose() {
    local out=$1
    if printf '%s' "$out" | grep -q 'buffer overflow'; then
        # Hardcoded 64-byte path buffer. Measured: a TMPDIR of 63 characters
        # works, 64 aborts. See the path-length check in probe.sh.
        echo "crashed: path too long (TMPDIR must be 63 characters or fewer)"
        return 0
    fi
    if printf '%s' "$out" | grep -q 'Unable to open'; then
        local f
        f=$(printf '%s' "$out" | grep -m1 'Unable to open' | sed 's/.*file //')
        echo "data files missing (${f##*/resources/tools/}); re-run: ./install.sh --only tools --force"
        return 0
    fi
    echo "ran but produced no prediction rows"
    return 0
}

veda::_t_netmhcpan() {
    local d="$VEDA_TOOLS_DIR/netMHCpan-4.2" w=$VEDA_VERIFY_WORK
    [[ -x "$d/netMHCpan" ]] || skip "not installed" || return 77
    local out n
    out=$(cd "$d" && TMPDIR="$w" ./netMHCpan -p "$w/pep9.txt" -a HLA-A02:01 2>&1) || true

    # A real row looks like:  1 HLA-A*02:01  SIINFEKLL  SIINFEKLL  0 ...
    n=$(printf '%s' "$out" | grep -cE '^[[:space:]]*[0-9]+[[:space:]]+HLA-[A-Z]\*[0-9]' || true)
    if (( n == 0 )); then
        veda::_netmhc_diagnose "$out"
        return 1
    fi
    echo "predicted $n peptide-allele rows"
}

veda::_t_netmhciipan() {
    local d="$VEDA_TOOLS_DIR/netMHCIIpan-4.3" w=$VEDA_VERIFY_WORK
    [[ -x "$d/netMHCIIpan" ]] || skip "not installed" || return 77
    local out n
    out=$(cd "$d" && TMPDIR="$w" ./netMHCIIpan -f "$w/pep15.fa" -a DRB1_0101 2>&1) || true

    # A real row looks like:  1  DRB1_0101  AAGIGILTVILGVLL  3  IGILTVILG ...
    # The banner also contains "DRB1_0101", so the position prefix is what
    # distinguishes a result from an echo of the request.
    n=$(printf '%s' "$out" | grep -cE '^[[:space:]]*[0-9]+[[:space:]]+DRB1_' || true)
    if (( n == 0 )); then
        veda::_netmhc_diagnose "$out"
        return 1
    fi
    echo "predicted $n MHC-II binding rows"
}

veda::_t_gibbs() {
    local d="$VEDA_TOOLS_DIR/gibbscluster-2.0"
    [[ -f "$d/GibbsCluster-2.0e_SA.pl" ]] || skip "not installed" || return 77
    [[ -x "$d/bin/gibbs_cluster_1.2.2_DB.Linux_x86_64" ]] \
        || { echo "core binary missing or not executable"; return 1; }
    local out
    out=$(cd "$d" && GIBBS="$d" perl GibbsCluster-2.0e_SA.pl -h 2>&1 | head -5)
    printf '%s' "$out" | grep -qiE 'usage|gibbscluster|option' \
        || { echo "perl wrapper did not respond to -h"; return 1; }
    echo "wrapper + binary present"
}

veda::_t_seq2logo() {
    local d="$VEDA_TOOLS_DIR/seq2logo-2.2"
    [[ -f "$d/Seq2Logo.py" ]] || skip "not installed" || return 77
    local py; py=$(env_python veda-ui)
    [[ -x "$py" ]] || { echo "interpreter missing: $py"; return 1; }
    local out
    out=$(cd "$d" && "$py" Seq2Logo.py -h 2>&1 | head -5)
    printf '%s' "$out" | grep -qiE 'usage|seq2logo|option' \
        || { echo "Seq2Logo.py did not respond to -h: $(printf '%s' "$out" | tail -1)"; return 1; }
    echo "responds to -h"
}

veda::_t_deeploc() {
    local b; b=$(env_bin veda-ui deeploc2)
    [[ -x "$b" ]] || skip "not installed in veda-ui" || return 77
    # --help only: a real run downloads several GB of ESM weights.
    local out; out=$("$b" --help 2>&1 | head -5)
    printf '%s' "$out" | grep -qiE 'usage|deeploc|fasta' \
        || { echo "deeploc2 --help failed: $(printf '%s' "$out" | tail -1)"; return 1; }
    echo "console script works (weights download on first real run)"
}

# --- Raghava-lab predictors -------------------------------------------------

# These all share a shape: python <script> -i <fasta> -o <out>, run from the
# tool's own directory, using veda-tools' interpreter.
veda::_t_raghava() {
    local dir=$1 script=$2 py=$3
    local d="$VEDA_TOOLS_DIR/$dir"
    [[ -f "$d/$script" ]] || { skip "not installed"; return 77; }
    [[ -x "$py" ]] || { echo "interpreter missing: $py"; return 1; }
    local out
    # -I keeps the tool's own directory off sys.path: several of these ship a
    # stray json.py / typing.py that would shadow the stdlib.
    out=$(cd "$d" && timeout 300 "$py" -I "$script" -h 2>&1 | head -8)
    printf '%s' "$out" | grep -qiE 'usage|option|-i |input' \
        || { echo "did not respond to -h: $(printf '%s' "$out" | grep -viE '^\s*$' | tail -1)"; return 1; }
    echo "responds to -h"
}

veda::_t_algpred()    { veda::_t_raghava algpred2        algpred2.py    "$(env_python veda-tools)"; }
veda::_t_toxinpred()  { veda::_t_raghava toxinpred2      toxinpred2.py  "$(env_python veda-tools)"; }
veda::_t_il2pred()    { veda::_t_raghava il2pred-main    il2pred.py     "$(env_python veda-tools)"; }
veda::_t_il4pred()    { veda::_t_raghava il4pred2-main   il4pred2.py    "$(env_python veda-tools)"; }
veda::_t_ifnepitope() { veda::_t_raghava ifnepitope2-main ifnepitope2.py "$(env_python veda-ifn)"; }

# --- DeepTMHMM / Docker -----------------------------------------------------

veda::_t_biolib() {
    local py; py=$(env_python veda-tmhmm)
    [[ -x "$py" ]] || { echo "veda-tmhmm missing"; return 1; }
    "$py" -I -c 'import biolib; print("pybiolib", biolib.__version__ if hasattr(biolib,"__version__") else "ok")' 2>&1 | tail -1 \
        || { echo "biolib will not import"; return 1; }
}

veda::_t_image() {
    local image=$1
    have docker || { skip "docker not installed"; return 77; }
    docker info >/dev/null 2>&1 || { skip "docker daemon not reachable"; return 77; }
    if docker image inspect "$image" >/dev/null 2>&1; then
        docker image inspect "$image" --format '{{.Size}}' 2>/dev/null \
            | awk '{printf "%.1f GiB local\n", $1/1073741824}'
        return 0
    fi
    skip "image not pulled"; return 77
}

veda::_t_vaxign_image() { veda::_t_image "e4ong1031/vaxign-ml:latest"; }
# DeepTMHMM's local Docker path, which has no image anyone can pull.
#
# Reported as "unavailable by design" rather than a failure: cloud mode via
# pybiolib is the supported route and is checked separately. See the header of
# installer/lib/dockerize.sh for why no image exists.
veda::_t_tmhmm_image() {
    local id
    # tmhmm_local_worker.py's hardcoded image ID, present only if some earlier
    # biolib run left that layer in this machine's Docker cache.
    for id in "f18bfda34274" "biolib/dtu-deeptmhmm:latest"; do
        if have docker && docker image inspect "$id" >/dev/null 2>&1; then
            echo "local image $id present; the Docker path will work"
            return 0
        fi
    done
    skip "no public image exists; DeepTMHMM runs in cloud mode via pybiolib" || return 77
}

veda::_t_launcher() {
    # The command is named after VEDA_HOME, so a trial install in
    # ~/.veda-test is called `veda-test` and does not clobber a real `veda`.
    # This check must use the same name the launcher stage generated, not a
    # hardcoded "veda".
    local cmd=${VEDA_CMD:-veda}
    [[ -x "$VEDA_HOME/bin/$cmd" ]] || { echo "launcher missing: $VEDA_HOME/bin/$cmd"; return 1; }
    if [[ -L "$HOME/.local/bin/$cmd" ]]; then
        echo "$VEDA_HOME/bin/$cmd (+ ~/.local/bin/$cmd)"
    else
        echo "$VEDA_HOME/bin/$cmd"
    fi
}

# --- report -----------------------------------------------------------------

veda::_verify_report() {
    printf '\n'
    printf '   %s%d passed%s, %s%d failed%s, %s%d unavailable%s\n' \
        "$C_GREEN" "$VEDA_CHK_PASS" "$C_RESET" \
        "$( ((VEDA_CHK_FAIL)) && printf '%s' "$C_RED" || printf '%s' "$C_DIM")" "$VEDA_CHK_FAIL" "$C_RESET" \
        "$C_YELLOW" "$VEDA_CHK_SKIP" "$C_RESET"

    if (( VEDA_CHK_SKIP > 0 )); then
        printf '\n   %sUnavailable steps%s (VEDA runs; these stay greyed out):\n' "$C_BOLD" "$C_RESET"
        local row
        for row in "${VEDA_CHK_REPORT[@]}"; do
            IFS='|' read -r st _cls label detail <<<"$row"
            [[ "$st" == SKIP ]] && printf '     ○ %-22s %s\n' "$label" "$detail"
        done
    fi

    if (( VEDA_CHK_FAIL > 0 )); then
        printf '\n   %sFailures%s:\n' "$C_BOLD$C_RED" "$C_RESET"
        local row
        for row in "${VEDA_CHK_REPORT[@]}"; do
            IFS='|' read -r st _cls label detail <<<"$row"
            [[ "$st" == FAIL ]] && printf '     ✘ %-22s %s\n' "$label" "$detail"
        done
        printf '\n   Full log: %s\n' "${VEDA_LOG:-<none>}"
        return 1
    fi

    log_ok "verification complete"
    return 0
}
