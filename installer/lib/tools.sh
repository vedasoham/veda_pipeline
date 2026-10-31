#!/usr/bin/env bash
# VEDA installer :: stage 5 -- acquire, unpack and repair the prediction tools.
#
# Reads installer/manifest/tools.manifest and, for each tool, makes sure the
# directory under resources/tools/ exists and contains its sentinel file.
#
#   kind=free / kind=lfs   cloned from GitHub without asking
#   kind=dtu               licence-gated. Used from the bundle if already
#                          present; otherwise the installer prints the exact
#                          registration URL and waits for a tarball to be
#                          dropped into $VEDA_STAGING.
#   kind=docker            handled in dockerize.sh, skipped here
#
# Every tool then goes through a post-install hook that fixes the things these
# archives reliably get wrong: AppleDouble litter, lost execute bits, model
# archives left zipped, and missing BLAST indexes.

# Filled in as we go, consumed by the summary and the doctor.
declare -gA VEDA_TOOL_STATUS=()
declare -gA VEDA_TOOL_LABEL=()
declare -gA VEDA_TOOL_HELP=()
declare -ga VEDA_TOOLS_MISSING=()

veda::tools_install() {
    stage_header "Prediction tools"

    local manifest="$VEDA_INSTALLER_DIR/manifest/tools.manifest"
    [[ -f "$manifest" ]] || die "tool manifest missing: $manifest"

    mkdir -p "$VEDA_TOOLS_DIR" "$VEDA_STAGING"
    veda::mirror_init

    local id kind dir sentinel source help label
    while IFS='|' read -r id kind dir sentinel source help label; do
        [[ -z "${id// }" || "${id:0:1}" == '#' ]] && continue
        # Trim surrounding whitespace on every field.
        id=${id// /}; kind=${kind// /}
        dir=$(veda::_trim "$dir"); sentinel=$(veda::_trim "$sentinel")
        source=$(veda::_trim "$source"); help=$(veda::_trim "$help")
        label=$(veda::_trim "$label")

        [[ "$kind" == docker ]] && continue
        if [[ -n "${VEDA_ONLY_TOOL:-}" && "$VEDA_ONLY_TOOL" != "$id" ]]; then continue; fi

        VEDA_TOOL_LABEL[$id]=$label
        VEDA_TOOL_HELP[$id]=$help
        veda::_tool_one "$id" "$kind" "$dir" "$sentinel" "$source" "$help" "$label"
    done <"$manifest"

    veda::_tools_summary
}

# veda::_trim lives in common.sh -- mirror.sh needs it too and is sourced first.

# --- per-tool driver --------------------------------------------------------

veda::_tool_one() {
    local id=$1 kind=$2 dir=$3 sentinel=$4 source=$5 help=$6 label=$7
    local path="$VEDA_TOOLS_DIR/$dir"

    # Resolution order: already here > hand-staged archive > mirror > upstream
    # clone > ask the user. The mirror sits ahead of the clone on purpose: it
    # is checksum-verified and does not depend on git-lfs resolving.
    if veda::_tool_present "$path" "$sentinel"; then
        log_ok "$label -- present"
        VEDA_TOOL_STATUS[$id]=present
    elif veda::_tool_from_staging "$id" "$path" "$dir" "$sentinel"; then
        VEDA_TOOL_STATUS[$id]=staged
    elif veda::mirror_configured && veda::mirror_fetch "$id" "$path"; then
        VEDA_TOOL_STATUS[$id]=mirror
    else
        case "$kind" in
            free|lfs)
                log_info "$label -- missing, downloading from upstream"
                if veda::_tool_clone "$id" "$path" "$source" "$kind"; then
                    VEDA_TOOL_STATUS[$id]=downloaded
                else
                    log_err "$label -- download failed"
                    VEDA_TOOL_STATUS[$id]=failed
                    VEDA_TOOLS_MISSING+=("$id")
                    return 0
                fi
                ;;
            archive)
                # A direct archive URL, for a tool whose git repository does
                # not carry everything it needs. See the manifest comment for
                # toxinpred2: its RF_model is absent from GitHub entirely and
                # ships only in the author's standalone package.
                log_info "$label -- missing, downloading the standalone package"
                if veda::_tool_from_url "$id" "$path" "$dir" "$sentinel" "$source"; then
                    VEDA_TOOL_STATUS[$id]=downloaded
                else
                    log_err "$label -- download failed"
                    VEDA_TOOL_STATUS[$id]=failed
                    VEDA_TOOLS_MISSING+=("$id")
                    return 0
                fi
                ;;
            dtu)
                veda::_tool_dtu_guidance "$id" "$label" "$help" "$dir"
                if veda::_tool_present "$path" "$sentinel"; then
                    VEDA_TOOL_STATUS[$id]=staged
                else
                    VEDA_TOOL_STATUS[$id]=absent
                    VEDA_TOOLS_MISSING+=("$id")
                    return 0
                fi
                ;;
            *)
                log_warn "$label -- unknown kind '$kind' in manifest; skipping"
                VEDA_TOOL_STATUS[$id]=unknown
                return 0
                ;;
        esac
    fi

    # Repairs run on every pass, including for tools that were already there.
    veda::_tool_repair "$id" "$path"

    if ! veda::_tool_present "$path" "$sentinel"; then
        log_err "$label -- sentinel '$sentinel' still missing after setup"
        VEDA_TOOL_STATUS[$id]=broken
        VEDA_TOOLS_MISSING+=("$id")
        return 0
    fi

    # The sentinel proves the launcher script arrived. It does NOT prove the
    # data payload did, and for the DTU tools the payload is most of the
    # download: netMHCpan ships a 190 MB data/ tree, netMHCIIpan a 125 MB one,
    # both separate from the wrapper. A directory that has the wrapper and not
    # the data looks completely healthy here and then dies at run time with
    # "Unable to open(r) file .../data/version" -- while still exiting 0.
    if ! veda::_tool_data_ok "$id" "$path"; then
        VEDA_TOOL_STATUS[$id]=incomplete
        VEDA_TOOLS_MISSING+=("$id")
    fi
}

# Per-tool data payload check, for tools whose data is a separate download.
#
# Only the tools that actually have a detachable payload are listed; anything
# else returns success. Paths were taken from the error each tool emits when
# the file is absent.
veda::_tool_data_ok() {
    local id=$1 path=$2 missing=()

    case $id in
        netmhcpan)
            [[ -s "$path/Linux_x86_64/data/version" ]] || missing+=("Linux_x86_64/data/")
            ;;
        netmhciipan)
            [[ -s "$path/data/version" ]] || missing+=("data/")
            ;;
        *) return 0 ;;
    esac

    (( ${#missing[@]} )) || return 0

    log_err "${id}: the program is present but its data payload is missing (${missing[*]})"
    log_err "  the tool will exit 0 and produce no predictions until this is fixed"
    log_err "  re-stage it from the original download, or: ./install.sh --only tools --only-tool $id --force"
    return 1
}

# Download an archive by URL, then reuse the staging unpack path.
#
# The file is saved into $VEDA_STAGING under a name containing the tool id, so
# veda::_tool_from_staging's own glob finds it. That means one implementation
# of "unpack an archive into resources/tools" rather than two, and a partially
# downloaded tool is resumable across runs.
veda::_tool_from_url() {
    local id=$1 path=$2 dir=$3 sentinel=$4 url=$5

    [[ -n "$url" && "$url" != '-' ]] || { log_err "$id: no archive URL in the manifest"; return 1; }
    if [[ "${VEDA_OFFLINE:-0}" == 1 ]]; then
        log_warn "$id: offline mode, cannot download $url"
        return 1
    fi

    mkdir -p "$VEDA_STAGING"
    local dest="$VEDA_STAGING/${id}-${url##*/}"

    if [[ -s "$dest" ]]; then
        log_info "$id: reusing already-downloaded $(basename "$dest")"
    else
        if ! retry 3 "download $id" curl -fL --connect-timeout 20 -o "$dest.part" "$url"; then
            rm -f "$dest.part"
            log_err "$id: could not download $url"
            return 1
        fi
        # A web server that answers a missing file with a styled 200 page would
        # otherwise be unpacked as if it were the tool.
        if head -c 512 "$dest.part" 2>/dev/null | grep -qiE '<!doctype html|<html'; then
            log_err "$id: $url returned an HTML page, not an archive"
            rm -f "$dest.part"
            return 1
        fi
        mv -f "$dest.part" "$dest"
        log_ok "$id: downloaded $(basename "$dest") ($(du -h "$dest" | awk '{print $1}'))"
    fi

    veda::_tool_from_staging "$id" "$path" "$dir" "$sentinel"
}

# Resolve git-lfs pointer stubs that `git lfs pull` left behind.
#
# Why this is needed at all: a repository can commit LFS pointers WITHOUT a
# .gitattributes declaring the `filter=lfs` attribute for those paths. Git then
# has no smudge filter to apply, so `git lfs pull` and `git lfs checkout`
# report success -- exit 0, no output -- and leave every file as a 130-byte
# pointer. raghavagps/ifnepitope2 is in exactly this state: 13 of its files,
# including the main script and model.zip, stay stubs after a clean clone.
#
# The content is not lost. `git lfs fetch` downloads the objects into
# .git/lfs/objects/<ab>/<cd>/<oid> regardless; only the checkout step fails.
# So: fetch, then for each remaining stub read its oid and copy the object
# over the pointer.
#
# Every object is verified by SHA-256 against the oid in its own pointer
# before being trusted. The oid IS the content hash, so this is a real
# integrity check, not a formality -- and these files are downloaded code that
# is about to be executed.
veda::_lfs_resolve() {
    local id=$1 repo=$2
    local fixed=0 unresolved=0 corrupt=0

    # Make sure the objects are local even when the smudge filter never ran.
    run "git lfs fetch ($id)" git -C "$repo" lfs fetch --all >/dev/null 2>&1 || true

    local f oid want obj actual
    while IFS= read -r -d '' f; do
        head -c 48 "$f" 2>/dev/null | grep -q 'version https://git-lfs' || continue

        oid=$(awk -F'sha256:' '/^oid sha256:/{print $2; exit}' "$f" 2>/dev/null | tr -d '[:space:]')
        want=$(awk '/^size /{print $2; exit}' "$f" 2>/dev/null | tr -d '[:space:]')
        [[ -n "$oid" ]] || { unresolved=$((unresolved+1)); continue; }

        obj="$repo/.git/lfs/objects/${oid:0:2}/${oid:2:2}/$oid"
        [[ -f "$obj" ]] || { unresolved=$((unresolved+1)); continue; }

        actual=$(sha256sum "$obj" 2>/dev/null | awk '{print $1}')
        if [[ "$actual" != "$oid" ]]; then
            log_warn "$id: LFS object for ${f#$repo/} is corrupt (sha256 mismatch); leaving the pointer"
            corrupt=$((corrupt+1)); continue
        fi
        if [[ -n "$want" && "$(stat -c%s "$obj" 2>/dev/null)" != "$want" ]]; then
            log_warn "$id: LFS object for ${f#$repo/} has the wrong size; leaving the pointer"
            corrupt=$((corrupt+1)); continue
        fi

        # Redirect into the existing file rather than cp, so the working
        # tree's mode (notably the executable bit) is preserved.
        if cat "$obj" >"$f" 2>/dev/null; then
            fixed=$((fixed+1))
        else
            unresolved=$((unresolved+1))
        fi
    # -size -4096c, in BYTES, not -1k.
    #
    # `find -size -1k` rounds every file up to whole 1 KiB blocks before
    # comparing, so a 130-byte pointer counts as 1 block and "-1k" (strictly
    # fewer than 1) matches only genuinely empty files. The first version of
    # this loop used -1k and silently examined nothing: the objects were
    # downloaded, the pointers stayed, and the only symptom was the tool still
    # being reported missing. Pointers are ~130 bytes; 4 KiB is generous.
    done < <(find "$repo" -type f -size -4096c -not -path '*/.git/*' -print0 2>/dev/null)

    if (( fixed )); then
        log_ok "$id: resolved $fixed git-lfs file(s) that git left as pointers"
    fi
    if (( unresolved || corrupt )); then
        log_warn "$id: $unresolved pointer(s) unresolved, $corrupt rejected as corrupt"
    fi
    return 0
}

veda::_tool_present() {
    local path=$1 sentinel=$2
    [[ -d "$path" ]] || return 1
    [[ "$sentinel" == '-' ]] && return 0
    [[ -e "$path/$sentinel" ]] || return 1
    # A git-lfs pointer is a ~130 byte text stub, not the model. Catch it.
    if [[ -f "$path/$sentinel" ]] && head -c 48 "$path/$sentinel" 2>/dev/null \
            | grep -q 'version https://git-lfs'; then
        log_warn "${path##*/}/$sentinel is an unresolved git-lfs pointer"
        return 1
    fi
    return 0
}

# --- public repos -----------------------------------------------------------

veda::_tool_clone() {
    local id=$1 path=$2 source=$3 kind=$4
    have git || { log_err "git not available; cannot download $id"; return 1; }

    if [[ "${VEDA_OFFLINE:-0}" == 1 ]]; then
        log_err "offline mode: cannot download $id from $source"
        return 1
    fi

    local tmp; tmp=$(mktemp -d "$VEDA_STAGING/clone-$id.XXXXXX")
    local rc=0

    # GIT_LFS_SKIP_SMUDGE=0 is the default, but be explicit: these repos are
    # useless without their model blobs.
    if ! retry 3 "clone $id" env GIT_LFS_SKIP_SMUDGE=0 GIT_TERMINAL_PROMPT=0 \
            git clone --depth 1 "$source" "$tmp/repo"; then
        log_err "could not clone $source"
        rm -rf "$tmp"; return 1
    fi

    if [[ "$kind" == lfs ]]; then
        if have git-lfs; then
            run "git lfs pull ($id)" git -C "$tmp/repo" lfs pull \
                || log_warn "git lfs pull failed for $id; trying a manual resolve"
            veda::_lfs_resolve "$id" "$tmp/repo"
        else
            log_warn "git-lfs missing: $id model files will be pointer stubs"
            log_warn "install it (sudo apt-get install git-lfs) and re-run with --force"
        fi
    fi

    # Move into place, keeping any existing directory as a backup.
    if [[ -d "$path" ]]; then
        local keep="$path.replaced-$(date +%Y%m%d%H%M%S)"
        mv "$path" "$keep"
        log_warn "existing $(basename "$path") moved to $(basename "$keep")"
    fi
    mkdir -p "$(dirname "$path")"
    mv "$tmp/repo" "$path" || rc=1
    rm -rf "$tmp"

    (( rc == 0 )) || { log_err "could not move $id into $path"; return 1; }
    log_ok "$id downloaded to resources/tools/$(basename "$path")"
    return 0
}

# --- licence-gated tools ----------------------------------------------------

veda::_tool_from_staging() {
    local id=$1 path=$2 dir=$3 sentinel=$4
    shopt -s nullglob nocaseglob
    local candidates=( "$VEDA_STAGING/"*"${id}"*.tar.gz "$VEDA_STAGING/"*"${id}"*.tgz \
                       "$VEDA_STAGING/"*"${id}"*.tar.Z  "$VEDA_STAGING/"*"${id}"*.tar \
                       "$VEDA_STAGING/"*"${id}"*.zip \
                       "$VEDA_STAGING/"*"${dir}"*.tar.gz "$VEDA_STAGING/"*"${dir}"*.zip )
    shopt -u nullglob nocaseglob

    (( ${#candidates[@]} > 0 )) || return 1

    local archive=${candidates[0]}
    log_info "found staged archive: $(basename "$archive")"

    local tmp; tmp=$(mktemp -d "$VEDA_STAGING/unpack-$id.XXXXXX")
    local ok=1
    case "$archive" in
        *.zip)            run "unzip $id"  unzip -q -o "$archive" -d "$tmp" || ok=0 ;;
        *.tar.Z|*.tar)    run "untar $id"  tar -xf "$archive" -C "$tmp"     || ok=0 ;;
        *.tar.gz|*.tgz)   run "untar $id"  tar -xzf "$archive" -C "$tmp"    || ok=0 ;;
    esac
    (( ok )) || { log_err "could not unpack $(basename "$archive")"; rm -rf "$tmp"; return 1; }

    # The archive usually contains one top-level directory; use it if so.
    local inner
    mapfile -t inner < <(find "$tmp" -mindepth 1 -maxdepth 1 -type d)
    local src="$tmp"
    (( ${#inner[@]} == 1 )) && src="${inner[0]}"

    mkdir -p "$path"
    if ! run "install $id files" cp -a "$src/." "$path/"; then
        log_err "could not copy $id into $path"; rm -rf "$tmp"; return 1
    fi
    rm -rf "$tmp"
    log_ok "$id unpacked into resources/tools/$(basename "$path")"
    return 0
}

veda::_tool_dtu_guidance() {
    local id=$1 label=$2 help=$3 dir=$4
    printf '\n'
    log_warn "$label is NOT installed and cannot be downloaded automatically."
    cat <<EOF

   ${C_BOLD}Why:${C_RESET} DTU Health Tech issues a personal, single-use download link
   after academic registration. Its licence forbids redistribution, so no
   script may fetch it on your behalf.

   ${C_BOLD}What to do:${C_RESET}
     1. Open   ${C_CYAN}${help}${C_RESET}
     2. Complete the academic licence form (free for academic use).
     3. Download the ${C_BOLD}Linux${C_RESET} archive from the emailed link.
     4. Drop the archive, unmodified, into:
          ${C_BOLD}${VEDA_STAGING}${C_RESET}
        Do not rename or unpack it: the installer finds it by name, then
        rewrites its internal paths and fixes its permissions for you.
     5. Re-run:  ${C_BOLD}./install.sh --only tools${C_RESET}

   Already extracted it elsewhere? Move or symlink the folder to:
          ${C_BOLD}${VEDA_TOOLS_DIR}/${dir}${C_RESET}

   Full instructions for all five DTU tools, and what to do when one
   misbehaves:
          ${C_BOLD}resources/tools/DTU_TOOLS.md${C_RESET}

EOF
    veda::_log_raw "GUIDANCE $id not installed; user must stage from $help"

    if [[ "${VEDA_ASSUME_YES:-0}" != 1 && -t 0 ]]; then
        if confirm "Pause so you can stage $label now?" n; then
            pause_for "Press Enter once the archive is in $VEDA_STAGING ..." || true
            if veda::_tool_from_staging "$id" "$VEDA_TOOLS_DIR/$dir" "$dir" '-'; then
                VEDA_TOOL_STATUS[$id]=staged
                return 0
            fi
            log_warn "still nothing usable in $VEDA_STAGING; continuing without $label"
        fi
    fi
    return 1
}

# --- repairs ----------------------------------------------------------------

veda::_tool_repair() {
    local id=$1 path=$2
    [[ -d "$path" ]] || return 0

    strip_mac_cruft "$path"
    veda::_tool_fix_exec "$path"

    # Tool-specific fix-ups.
    case "$id" in
        ifnepitope2) veda::_fix_ifnepitope "$path" ;;
        algpred2)    veda::_fix_blastdb "$path" "Database/data" ;;
        toxinpred2)  veda::_fix_blastdb "$path" "Database/data" ;;
        il2pred)     veda::_fix_merci "$path/merci" ;;
        netmhcpan|netmhciipan|gibbscluster) veda::_fix_dtu_binaries "$path" ;;
        seq2logo)    veda::_fix_seq2logo "$path" ;;
        vaxign)      veda::_fix_vaxign "$path" ;;
    esac
}

veda::_tool_fix_exec() {
    local path=$1
    # Perl/shell entry points and anything under bin/ or progs/ must be +x.
    find "$path" -maxdepth 2 -type f \( -name '*.pl' -o -name '*.sh' \) \
        -exec chmod u+x {} + 2>/dev/null || true
    local d
    for d in bin progs Linux_x86_64/bin; do
        [[ -d "$path/$d" ]] && chmod -R u+x "$path/$d" 2>/dev/null || true
    done
    return 0
}

veda::_fix_ifnepitope() {
    local path=$1
    # The repo ships model.zip rather than an unpacked model/ directory.
    if [[ -f "$path/model.zip" ]] && [[ ! -d "$path/model" || -z "$(ls -A "$path/model" 2>/dev/null)" ]]; then
        if head -c 48 "$path/model.zip" | grep -q 'version https://git-lfs'; then
            log_warn "ifnepitope2 model.zip is an unresolved git-lfs pointer"
            return 0
        fi
        log_info "unpacking ifnepitope2 model.zip"
        mkdir -p "$path/model"
        run "unzip ifnepitope model" unzip -q -o "$path/model.zip" -d "$path" \
            || log_warn "could not unpack model.zip"
    fi
    [[ -d "$path/model" ]] && log_debug "ifnepitope2 model/ present"
    return 0
}

veda::_fix_blastdb() {
    local path=$1 db=$2
    local full="$path/$db"
    [[ -f "$full.psq" ]] && { log_debug "${path##*/}: BLAST db present"; return 0; }

    # Rebuild from a bundled FASTA when the index is absent.
    local fasta
    for fasta in "$full" "$full.fasta" "$full.fa" "$path/Database/"*.fasta; do
        [[ -f "$fasta" ]] || continue
        head -1 "$fasta" 2>/dev/null | grep -q '^>' || continue
        local mkb; mkb=$(env_bin veda-tools makeblastdb)
        [[ -x "$mkb" ]] || { log_warn "makeblastdb unavailable; cannot build ${path##*/} BLAST db"; return 0; }
        log_info "building BLAST database for ${path##*/}"
        run "makeblastdb ${path##*/}" "$mkb" -in "$fasta" -dbtype prot -out "$full" \
            && { log_ok "${path##*/}: BLAST db built"; return 0; }
        log_warn "makeblastdb failed for ${path##*/}"
        return 0
    done
    log_warn "${path##*/}: no BLAST index and no source FASTA at $db"
    log_warn "the hybrid (BLAST+motif) prediction mode will not work"
    return 0
}

veda::_fix_merci() {
    local dir=$1
    [[ -d "$dir" ]] || return 0
    chmod u+x "$dir"/*.pl 2>/dev/null || true
    return 0
}

veda::_fix_dtu_binaries() {
    local path=$1
    # DTU tarballs frequently lose the execute bit in transit.
    local f
    for f in "$path"/netMHCpan "$path"/netMHCIIpan "$path"/gibbscluster \
             "$path"/GibbsCluster-2.0e_SA.pl; do
        [[ -f "$f" ]] && chmod u+x "$f" 2>/dev/null || true
    done
    [[ -d "$path/Linux_x86_64/bin" ]] && chmod -R u+x "$path/Linux_x86_64/bin" 2>/dev/null || true
    [[ -d "$path/bin" ]] && chmod -R u+x "$path/bin" 2>/dev/null || true
    return 0
}

veda::_fix_seq2logo() {
    local path=$1
    [[ -f "$path/Seq2Logo.py" ]] && chmod u+x "$path/Seq2Logo.py" 2>/dev/null || true
    # Stale .pyc from a Python 2 era install shadows the module on some setups.
    rm -f "$path/etc/Seq2Logo_module.pyc" 2>/dev/null || true
    return 0
}

veda::_fix_vaxign() {
    local path=$1
    local f
    for f in "$path"/*.sh; do [[ -f "$f" ]] && chmod u+x "$f"; done
    return 0
}

# --- summary ----------------------------------------------------------------

veda::_tools_summary() {
    printf '\n'
    log_info "tool inventory:"
    local id
    for id in "${!VEDA_TOOL_STATUS[@]}"; do :; done
    # Stable, manifest-ish ordering for readability.
    for id in algpred2 toxinpred2 il2pred il4pred2 ifnepitope2 vaxign \
              netmhcpan netmhciipan gibbscluster seq2logo deeploc; do
        [[ -n "${VEDA_TOOL_STATUS[$id]:-}" ]] || continue
        local st=${VEDA_TOOL_STATUS[$id]} mark colour
        case "$st" in
            present|downloaded|staged|mirror) mark='✔'; colour=$C_GREEN ;;
            absent)                           mark='○'; colour=$C_YELLOW ;;
            *)                                mark='✘'; colour=$C_RED ;;
        esac
        local origin=''
        case "$st" in
            mirror)     origin=' (from mirror)' ;;
            downloaded) origin=' (cloned)' ;;
            staged)     origin=' (staged)' ;;
        esac
        printf '     %s%s%s %-14s %s%s\n' "$colour" "$mark" "$C_RESET" "$id" \
            "${VEDA_TOOL_LABEL[$id]:-}" "$origin"
    done

    if (( ${#VEDA_TOOLS_MISSING[@]} > 0 )); then
        printf '\n'
        log_warn "${#VEDA_TOOLS_MISSING[@]} tool(s) unavailable: ${VEDA_TOOLS_MISSING[*]}"
        log_warn "VEDA will install and run; the matching pipeline steps stay disabled"
        log_warn "re-run ./install.sh after staging them to enable those steps"
    else
        log_ok "all prediction tools accounted for"
    fi
}
