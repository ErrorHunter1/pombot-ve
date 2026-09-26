install_docker
log "Starte Uptime Kuma …"
docker rm -f uptime-kuma >/dev/null 2>&1 || true
docker run -d --name uptime-kuma --restart=always -p 3001:3001 -v uptime-kuma:/app/data louislam/uptime-kuma:1
info "Uptime Kuma: http://${HOST}:3001"
info "Beim ersten Aufruf legst du das Admin-Konto an."
log "Fertig."
