#!/usr/bin/env bash
# VEDA installer :: stage 6 -- Docker engine and container images.
#
# One pipeline step runs in a container:
#   Vaxign-ML   e4ong1031/vaxign-ml:latest   (protegenicity scoring)
#
# Docker is optional. Without it the rest of VEDA works and that step reports
# itself unavailable, so nothing here is allowed to be fatal.
#
# DeepTMHMM is deliberately NOT here.
#
# There is no public DeepTMHMM image to pull. `biolib/dtu-deeptmhmm` is not a
# real Docker Hub repository -- an anonymous pull returns "repository does not
# exist or may require 'docker login'" -- and DTU does not publish one; the
# supported route is pybiolib, which VEDA installs and verifies. The app's
# backend/workers/tmhmm_local_worker.py refers to a bare image ID
# ("f18bfda34274"), which only resolves on a machine where some earlier biolib
# run happened to leave that layer in the local Docker cache. It is a
# developer-machine artefact, not something an installer can reproduce.
#
# So: DeepTMHMM runs in cloud mode through pybiolib. The local Docker path
# stays unavailable unless the user supplies an image themselves.
VEDA_DOCKER_IMAGES=(
    "e4ong1031/vaxign-ml:latest"
)

VEDA_DOCKER_OK=0
VEDA_DOCKER_NEEDS_RELOGIN=0

veda::docker_setup() {
    # Works when this stage runs on its own (./install.sh --only docker).
    veda::probe_ensure

    stage_header "Docker (Vaxign-ML, DeepTMHMM)"

    if [[ "${VEDA_SKIP_DOCKER:-0}" == 1 ]]; then
        log_info "--skip-docker given; Vaxign-ML and the local DeepTMHMM path stay disabled"
        return 0
    fi

    # Installing a Docker engine inside a container means fighting the host's
    # daemon over /var/run/docker.sock. Skip rather than make a mess; the two
    # tools that need it degrade cleanly.
    if (( ${VEDA_PROBE_IS_CONTAINER:-0} )); then
        log_warn "running inside a container; skipping the Docker stage"
        log_info "to use Vaxign-ML, mount the host's /var/run/docker.sock into this container"
        return 0
    fi

    # Podman can run these images, but the app shells out to `docker`.
    # podman-docker provides that name; we do not install it unasked.
    if [[ "${VEDA_PROBE_DOCKER:-absent}" == podman ]]; then
        log_warn "podman is present but docker is not; skipping the Docker stage"
        log_info "either install docker, or:  sudo apt-get install -y podman-docker"
        log_info "then re-run: ./install.sh --only docker"
        return 0
    fi

    # Nothing to install: the probe already proved the daemon answers.
    if [[ "${VEDA_PROBE_DOCKER:-absent}" == ok ]]; then
        log_ok "docker already usable; only the images are needed"
        VEDA_DOCKER_OK=1
        veda::_docker_images
        return 0
    fi

    # Works even when preflight was skipped on a resumed run.
    veda::ensure_sudo

    veda::_docker_engine || return 0
    veda::_docker_group
    veda::_docker_daemon || return 0
    veda::_docker_images
}

# --- engine -----------------------------------------------------------------

veda::_docker_engine() {
    if have docker; then
        log_ok "docker present ($(docker --version 2>/dev/null | sed 's/,.*//'))"
        return 0
    fi

    log_info "docker is not installed"
    if [[ "${VEDA_HAVE_SUDO:-0}" != 1 ]]; then
        log_warn "cannot install docker without sudo; skipping"
        return 1
    fi
    if ! confirm "Install Docker Engine from Docker's official apt repository?" y; then
        log_warn "skipping docker install at your request"
        return 1
    fi

    veda::_docker_apt_repo || return 1

    run "apt-get update (docker repo)" sudo apt-get update -y \
        || { log_warn "apt-get update failed after adding the docker repo"; return 1; }

    if run "install docker engine" sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
            docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; then
        log_ok "Docker Engine installed"
        return 0
    fi

    log_warn "the official docker packages failed; trying the distro's docker.io"
    if run "install docker.io" sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io; then
        log_ok "docker.io installed"
        return 0
    fi
    log_warn "could not install docker; Vaxign-ML will be unavailable"
    return 1
}

veda::_docker_apt_repo() {
    # Docker publishes repos for ubuntu and debian only; derivatives must be
    # mapped onto their upstream codename or the repo 404s.
    local repo_os codename
    case "${VEDA_OS_ID:-ubuntu}" in
        ubuntu) repo_os=ubuntu ;;
        debian|raspbian) repo_os=debian ;;
        linuxmint|pop|elementary|zorin) repo_os=ubuntu ;;
        *) repo_os=ubuntu ;;
    esac

    codename=$(. /etc/os-release 2>/dev/null; printf '%s' "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")
    if [[ -z "$codename" ]] && have lsb_release; then codename=$(lsb_release -cs 2>/dev/null); fi
    [[ -n "$codename" ]] || { log_warn "cannot determine the distro codename; skipping docker repo"; return 1; }

    log_info "adding docker apt repository ($repo_os/$codename)"
    run "mkdir keyrings" sudo install -m 0755 -d /etc/apt/keyrings || return 1

    if [[ ! -s /etc/apt/keyrings/docker.gpg ]]; then
        if ! curl -fsSL "https://download.docker.com/linux/$repo_os/gpg" \
                | sudo gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg; then
            log_warn "could not fetch Docker's signing key"
            return 1
        fi
        sudo chmod a+r /etc/apt/keyrings/docker.gpg
    fi

    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/%s %s stable\n' \
        "$(dpkg --print-architecture)" "$repo_os" "$codename" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    log_ok "docker repository configured"
}

# --- group membership -------------------------------------------------------

veda::_docker_group() {
    if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        log_ok "$USER is already in the 'docker' group"
        return 0
    fi

    if [[ "${VEDA_HAVE_SUDO:-0}" != 1 ]]; then
        log_warn "$USER is not in the 'docker' group and sudo is unavailable"
        log_warn "Vaxign-ML will need a password for every run"
        return 0
    fi

    log_info "adding $USER to the 'docker' group"
    getent group docker >/dev/null || run "create docker group" sudo groupadd docker || true
    if run "usermod -aG docker" sudo usermod -aG docker "$USER"; then
        VEDA_DOCKER_NEEDS_RELOGIN=1
        log_ok "$USER added to the 'docker' group"
        log_warn "this only takes effect in NEW login sessions"
        log_warn "log out and back in, or run 'newgrp docker' in this terminal"
    else
        log_warn "could not add $USER to the docker group"
    fi
}

# --- daemon -----------------------------------------------------------------

veda::_docker_daemon() {
    if docker info >/dev/null 2>&1; then
        log_ok "docker daemon reachable"
        VEDA_DOCKER_OK=1
        return 0
    fi

    # Try to start it; on WSL and in containers there may be no systemd.
    if have systemctl && [[ "${VEDA_HAVE_SUDO:-0}" == 1 ]]; then
        log_info "starting the docker service"
        run "enable docker" sudo systemctl enable --now docker || true
        sleep 3
        if docker info >/dev/null 2>&1; then
            log_ok "docker daemon started"
            VEDA_DOCKER_OK=1
            return 0
        fi
    fi

    if sudo -n docker info >/dev/null 2>&1; then
        log_warn "docker works only via sudo in this session (group change pending)"
        log_warn "image pulls will use sudo; after re-login they will not need it"
        VEDA_DOCKER_OK=2
        return 0
    fi

    log_warn "docker daemon not reachable; skipping image pulls"
    log_warn "Vaxign-ML and the local DeepTMHMM path will be unavailable"
    log_warn "once docker runs, re-enable them with: ./install.sh --only docker"
    return 1
}

veda::_dk() {
    # Route through sudo when the group change has not taken effect yet.
    if [[ "$VEDA_DOCKER_OK" == 2 ]]; then sudo docker "$@"; else docker "$@"; fi
}

# --- images -----------------------------------------------------------------

veda::_docker_images() {
    if [[ "${VEDA_OFFLINE:-0}" == 1 ]]; then
        log_warn "offline mode: skipping image pulls"
        return 0
    fi

    local image pulled=0 failed=0
    for image in "${VEDA_DOCKER_IMAGES[@]}"; do
        if veda::_dk image inspect "$image" >/dev/null 2>&1 && [[ "${VEDA_FORCE:-0}" != 1 ]]; then
            log_skip "image $image"
            pulled=$(( pulled + 1 ))
            continue
        fi
        log_info "pulling $image (this is a large download)"
        if retry 2 "pull $image" veda::_dk pull "$image"; then
            log_ok "pulled $image"
            pulled=$(( pulled + 1 ))
        else
            log_warn "could not pull $image"
            failed=$(( failed + 1 ))
        fi
    done

    log_info "docker images ready: $pulled, failed: $failed"
    (( failed == 0 )) || log_warn "the steps backed by the failed image(s) will be unavailable"
}
