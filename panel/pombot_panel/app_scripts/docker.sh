install_docker
docker run --rm hello-world >/dev/null && log "Docker funktioniert."
info "Docker $(docker --version | awk '{print $3}' | tr -d ,) und Docker Compose sind installiert."
info "Test: docker run --rm hello-world"
log "Fertig."
