#!/usr/bin/env bash
# VEDA installer :: mirror support -- fetch pre-packed tool archives.
#
# A mirror lets the installer download every tool from one place you control,
# instead of cloning six GitHub repos (and depending on git-lfs resolving
# correctly) and hand-staging the five licence-gated ones.
#
# Configure it in any of these, highest priority first:
#     ./install.sh --mirror <url>
#     VEDA_MIRROR=<url> ./install.sh
#     installer/mirror.conf          (MIRROR=<url>)
#
# Supported URL forms:
#
#   gh://owner/repo@tag        GitHub Release assets.  RECOMMENDED.
#                              Uses `gh release download` when the gh CLI is
#                              available, which also works for PRIVATE repos.
#                              Falls back to the public download URL otherwise.
#
#   https://host/path/         Any web server, S3, R2, B2, institutional space.
#                              Each tool is fetched from <base>/<filename>.
#                              Resumable, no auth unless you add it yourself.
#
#   gdrive://<folder-id>       Google Drive, via gdown. Works, but every VEDA
#                              archive is over Drive's ~25 MB virus-scan
#                              threshold and Drive imposes frequency-based
#                              quota lockouts (HTTP 403/429, up to ~24h).
#                              Not recommended for a shared lab.
#
#   rclone://remote:path       Any rclone remote (Drive, OneDrive, Dropbox,
#                              S3...). Goes through the provider's real API, so
#                              it avoids Drive's interstitial and public-link
#                              quota entirely. Needs `rclone config` once.
#
#   file:///path/to/bundle     A local or mounted directory: an NFS share, a
#                              USB drive, a lab fileserver. The only form that
#                              works with no network at all.
#
# Every download is checked against installer/manifest/mirror.manifest, which
# veda-pack generates. A size or SHA-256 mismatch is a hard failure: a
# truncated model file fails much more confusingly later on.

VEDA_MIRROR_MANIFEST=''   # set in veda::mirror_init
VEDA_MIRROR=${VEDA_MIRROR:-}

veda::mirror_init() {
    VEDA_MIRROR_MANIFEST="$VEDA_INSTALLER_DIR/manifest/mirror.manifest"

    # mirror.conf is the committed default; env and --mirror override it.
    if [[ -z "$VEDA_MIRROR" && -f "$VEDA_INSTALLER_DIR/mirror.conf" ]]; then
        local line
        while IFS= read -r line || [[ -n "$line" ]]; do
            line=${line%%#*}
            [[ "$line" =~ ^[[:space:]]*MIRROR[[:space:]]*=[[:space:]]*(.+)$ ]] || continue
            VEDA_MIRROR=$(veda::_trim "${BASH_REMATCH[1]}")
            VEDA_MIRROR=${VEDA_MIRROR%\"}; VEDA_MIRROR=${VEDA_MIRROR#\"}
            VEDA_MIRROR=${VEDA_MIRROR%\'}; VEDA_MIRROR=${VEDA_MIRROR#\'}
        done <"$VEDA_INSTALLER_DIR/mirror.conf"
        [[ -n "$VEDA_MIRROR" ]] && log_debug "mirror from mirror.conf: $VEDA_MIRROR"
    fi

    if [[ -z "$VEDA_MIRROR" ]]; then
        log_debug "no mirror configured"
        return 0
    fi
    if [[ "${VEDA_OFFLINE:-0}" == 1 ]]; then
        log_info "offline mode: ignoring the configured mirror"
        VEDA_MIRROR=''
        return 0
    fi

    log_info "mirror: $VEDA_MIRROR"
    if [[ ! -f "$VEDA_MIRROR_MANIFEST" ]]; then
        log_warn "mirror configured but $VEDA_MIRROR_MANIFEST is missing"
        log_warn "downloads cannot be checksum-verified; generate it with ./veda-pack"
    fi
}

veda::mirror_configured() { [[ -n "$VEDA_MIRROR" ]]; }

# --- manifest ---------------------------------------------------------------

# Look up a tool: echoes "filename|sha256|bytes", or nothing when absent.
veda::_mirror_lookup() {
    local want=$1
    [[ -f "$VEDA_MIRROR_MANIFEST" ]] || return 1
    local id file sha bytes
    while IFS='|' read -r id file sha bytes _rest; do
        [[ -z "${id// }" || "${id:0:1}" == '#' ]] && continue
        id=$(veda::_trim "$id")
        [[ "$id" == "$want" ]] || continue
        printf '%s|%s|%s' "$(veda::_trim "$file")" "$(veda::_trim "$sha")" "$(veda::_trim "$bytes")"
        return 0
    done <"$VEDA_MIRROR_MANIFEST"
    return 1
}

veda::_mirror_verify() {
    local file=$1 want_sha=$2 want_bytes=$3

    local got_bytes
    got_bytes=$(stat -c%s "$file" 2>/dev/null || echo 0)
    if [[ -n "$want_bytes" && "$want_bytes" != '-' && "$want_bytes" != "$got_bytes" ]]; then
        log_err "size mismatch for $(basename "$file"): expected $want_bytes bytes, got $got_bytes"
        log_err "the download was truncated or the mirror is out of date"
        return 1
    fi

    if [[ -z "$want_sha" || "$want_sha" == '-' ]]; then
        log_warn "no checksum recorded for $(basename "$file"); skipping integrity check"
        return 0
    fi
    have sha256sum || { log_warn "sha256sum unavailable; skipping integrity check"; return 0; }

    local got_sha
    got_sha=$(sha256sum "$file" | awk '{print $1}')
    if [[ "$got_sha" != "$want_sha" ]]; then
        log_err "checksum mismatch for $(basename "$file")"
        log_err "  expected $want_sha"
        log_err "  got      $got_sha"
        log_err "re-upload the archive, or regenerate the manifest with ./veda-pack"
        return 1
    fi
    log_debug "$(basename "$file") verified (sha256 ok, $got_bytes bytes)"
    return 0
}

# --- the entry point tools.sh calls -----------------------------------------

# veda::mirror_fetch <id> <dest_dir>
# Downloads, verifies and extracts the tool straight into <dest_dir>.
# Returns 1 if the tool is not on the mirror or anything fails.
veda::mirror_fetch() {
    local id=$1 dest=$2
    veda::mirror_configured || return 1

    local rec file sha bytes
    if rec=$(veda::_mirror_lookup "$id"); then
        IFS='|' read -r file sha bytes <<<"$rec"
    else
        # No manifest entry: fall back to the conventional name, unverified.
        file="$id.tar.gz"; sha='-'; bytes='-'
        if [[ -f "$VEDA_MIRROR_MANIFEST" ]]; then
            log_warn "$id has no entry in mirror.manifest; trying $file UNVERIFIED"
            log_warn "re-run ./veda-pack to record its checksum"
        else
            log_debug "$id not in mirror.manifest; trying $file unverified"
        fi
    fi

    local cache="$VEDA_STAGING/mirror"
    mkdir -p "$cache"
    local archive="$cache/$file"

    # Reuse an already-verified download (makes re-runs and --force cheap).
    if [[ -f "$archive" ]] && veda::_mirror_verify "$archive" "$sha" "$bytes" 2>/dev/null; then
        log_info "$id: using cached $file"
    else
        rm -f "$archive"
        log_info "$id: downloading $file from the mirror"
        veda::_mirror_download "$file" "$archive" || { rm -f "$archive"; return 1; }
        veda::_mirror_verify "$archive" "$sha" "$bytes" || { rm -f "$archive"; return 1; }
    fi

    veda::_mirror_extract "$archive" "$dest" || return 1
    log_ok "$id installed from the mirror"
    return 0
}

veda::_mirror_download() {
    local file=$1 out=$2
    case "$VEDA_MIRROR" in
        gh://*)      veda::_mirror_dl_gh      "$file" "$out" ;;
        https://*|http://*) veda::_mirror_dl_https "$file" "$out" ;;
        gdrive://*)  veda::_mirror_dl_gdrive  "$file" "$out" ;;
        rclone://*)  veda::_mirror_dl_rclone  "$file" "$out" ;;
        file://*)    veda::_mirror_dl_file    "$file" "$out" ;;
        /*)          VEDA_MIRROR="file://$VEDA_MIRROR"; veda::_mirror_dl_file "$file" "$out" ;;
        *)
            log_err "unrecognised mirror URL: $VEDA_MIRROR"
            log_err "expected gh://owner/repo@tag, https://..., gdrive://..., rclone://remote:path or file:///path"
            return 1 ;;
    esac
}

# --- backend: GitHub Releases ----------------------------------------------

veda::_mirror_dl_gh() {
    local file=$1 out=$2
    local spec=${VEDA_MIRROR#gh://}
    local repo tag
    if [[ "$spec" == *@* ]]; then repo=${spec%@*}; tag=${spec##*@}; else repo=$spec; tag='latest'; fi
    repo=${repo%/}

    # gh handles private repos and redirects; prefer it when usable.
    if have gh && gh auth status >/dev/null 2>&1; then
        local tmp; tmp=$(mktemp -d "$VEDA_STAGING/gh.XXXXXX")
        local args=(release download --repo "$repo" --pattern "$file" --dir "$tmp")
        [[ "$tag" != latest ]] && args=(release download "$tag" --repo "$repo" --pattern "$file" --dir "$tmp")
        if retry 3 "gh release download $file" gh "${args[@]}"; then
            if [[ -f "$tmp/$file" ]]; then
                mv -f "$tmp/$file" "$out"; rm -rf "$tmp"; return 0
            fi
        fi
        rm -rf "$tmp"
        log_warn "gh could not fetch $file from $repo@$tag; trying the public URL"
    elif have gh; then
        log_warn "gh is installed but not authenticated ('gh auth login')"
        log_warn "falling back to the public URL -- this fails for a private repo"
    fi

    # Public-release fallback: a plain, unauthenticated CDN URL.
    local url
    if [[ "$tag" == latest ]]; then
        url="https://github.com/$repo/releases/latest/download/$file"
    else
        url="https://github.com/$repo/releases/download/$tag/$file"
    fi
    veda::_mirror_curl "$url" "$out"
}

# --- backend: plain HTTPS ---------------------------------------------------

veda::_mirror_dl_https() {
    local file=$1 out=$2
    local base=${VEDA_MIRROR%/}
    veda::_mirror_curl "$base/$file" "$out"
}

veda::_mirror_curl() {
    local url=$1 out=$2
    have curl || { log_err "curl is required to download from $url"; return 1; }

    # --continue-at - resumes a partial file across retries.
    if retry 3 "download ${url##*/}" \
            curl -fL --retry 2 --connect-timeout 30 --continue-at - \
                 -o "$out" "$url"; then
        # A web server that answers 200 with an HTML error page is a classic
        # mirror misconfiguration; catch it before it becomes a tar error.
        if head -c 15 "$out" 2>/dev/null | grep -qi '<!doctype\|<html'; then
            log_err "$url returned an HTML page, not an archive"
            log_err "check the URL, and that the release asset is public or gh is authenticated"
            return 1
        fi
        return 0
    fi
    log_err "could not download $url"
    return 1
}

# --- backend: Google Drive (via gdown) --------------------------------------

veda::_mirror_dl_gdrive() {
    local file=$1 out=$2
    local folder=${VEDA_MIRROR#gdrive://}; folder=${folder%/}

    local py gdown
    py=$(env_python veda-ui 2>/dev/null || true)
    [[ -x "$py" ]] || { log_err "gdrive mirror needs the veda-ui environment; run the envs stage first"; return 1; }

    gdown=$(env_bin veda-ui gdown)
    if [[ ! -x "$gdown" ]]; then
        log_info "installing gdown (needed for Google Drive downloads)"
        retry 2 "pip install gdown" "$py" -m pip install --no-input gdown \
            || { log_err "could not install gdown"; return 1; }
    fi
    [[ -x "$gdown" ]] || { log_err "gdown still not available"; return 1; }

    # Drive has no path addressing, so the folder is listed and matched by name.
    local tmp; tmp=$(mktemp -d "$VEDA_STAGING/gdrive.XXXXXX")
    if retry 2 "gdown folder" "$gdown" --folder --remaining-ok -O "$tmp" \
            "https://drive.google.com/drive/folders/$folder"; then
        local found
        found=$(find "$tmp" -type f -name "$file" -print -quit 2>/dev/null || true)
        if [[ -n "$found" ]]; then
            mv -f "$found" "$out"; rm -rf "$tmp"; return 0
        fi
        log_err "$file not found in the Drive folder $folder"
    else
        log_err "gdown failed. Google Drive commonly blocks automated downloads with"
        log_err "a quota lockout (HTTP 403/429) that can last ~24h. Consider a"
        log_err "GitHub Release or plain HTTPS mirror instead."
    fi
    rm -rf "$tmp"
    return 1
}

# --- backend: local / mounted directory -------------------------------------

veda::_mirror_dl_file() {
    local file=$1 out=$2
    local base=${VEDA_MIRROR#file://}; base=${base%/}
    local src="$base/$file"

    [[ -d "$base" ]] || { log_err "mirror directory not found: $base"; return 1; }
    if [[ ! -f "$src" ]]; then
        log_err "$file is not in the mirror directory $base"
        return 1
    fi
    # Copy rather than symlink: the archive is deleted after a failed verify.
    run "copy $file from the local mirror" cp -f "$src" "$out" || return 1
    return 0
}

# --- backend: rclone --------------------------------------------------------

veda::_mirror_dl_rclone() {
    local file=$1 out=$2
    local remote=${VEDA_MIRROR#rclone://}; remote=${remote%/}

    have rclone || {
        log_err "rclone is not installed. Install it (https://rclone.org/install.sh)"
        log_err "and configure the remote once with: rclone config"
        return 1
    }
    local tmp; tmp=$(mktemp -d "$VEDA_STAGING/rclone.XXXXXX")
    if retry 3 "rclone copy $file" rclone copy "$remote/$file" "$tmp" --no-traverse; then
        if [[ -f "$tmp/$file" ]]; then
            mv -f "$tmp/$file" "$out"; rm -rf "$tmp"; return 0
        fi
    fi
    rm -rf "$tmp"
    log_err "rclone could not fetch $file from $remote"
    return 1
}

# --- extraction -------------------------------------------------------------
# veda-pack archives hold the tool's CONTENTS at the top level (tar -C dir .),
# so they extract straight into the destination with no nesting guesswork.

veda::_mirror_extract() {
    local archive=$1 dest=$2
    local name; name=$(basename "$archive")

    # Unpack to a staging directory first. A truncated archive can otherwise
    # leave a partly-extracted tool that satisfies the sentinel check and then
    # fails in confusing ways mid-analysis.
    local tmp; tmp=$(mktemp -d "$VEDA_STAGING/unpack.XXXXXX")
    local rc=0
    case "$archive" in
        *.tar.gz|*.tgz) run "extract $name" tar -xzf "$archive" -C "$tmp" || rc=1 ;;
        *.tar.zst)
            if have zstd; then
                run "extract $name" tar --use-compress-program=unzstd -xf "$archive" -C "$tmp" || rc=1
            else
                log_err "$name needs zstd: sudo apt-get install zstd"; rc=1
            fi ;;
        *.tar.xz)  run "extract $name" tar -xJf "$archive" -C "$tmp" || rc=1 ;;
        *.tar)     run "extract $name" tar -xf  "$archive" -C "$tmp" || rc=1 ;;
        *.zip)     run "extract $name" unzip -q -o "$archive" -d "$tmp" || rc=1 ;;
        *) log_err "unsupported archive type: $name"; rc=1 ;;
    esac

    if (( rc != 0 )); then
        rm -rf "$tmp"
        log_err "could not extract $name (the archive is corrupt or incomplete)"
        return 1
    fi
    if [[ -z "$(ls -A "$tmp" 2>/dev/null)" ]]; then
        rm -rf "$tmp"
        log_err "$name extracted to nothing"
        return 1
    fi

    mkdir -p "$dest"
    if ! run "install $name" cp -a "$tmp/." "$dest/"; then
        rm -rf "$tmp"
        log_err "could not move the extracted files into $dest"
        return 1
    fi
    rm -rf "$tmp"
    return 0
}
