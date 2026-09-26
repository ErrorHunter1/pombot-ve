install_docker
log "Richte Nginx Proxy Manager ein …"
mkdir -p /opt/nginx-proxy-manager
cat > /opt/nginx-proxy-manager/docker-compose.yml <<'EOF'
services:
  app:
    image: jc21/nginx-proxy-manager:latest
    restart: unless-stopped
    ports:
      - "80:80"
      - "81:81"
      - "443:443"
    volumes:
      - ./data:/data
      - ./letsencrypt:/etc/letsencrypt
EOF
cd /opt/nginx-proxy-manager
docker compose up -d
info "Nginx Proxy Manager: http://${HOST}:81"
info "Erste Anmeldung: admin@example.com / changeme (danach sofort ändern)."
info "Weitergeleitete Seiten laufen über die Ports 80 und 443."
info "Dateien: /opt/nginx-proxy-manager"
log "Fertig."
