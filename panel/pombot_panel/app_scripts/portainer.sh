install_docker
log "Starte Portainer CE …"
docker volume create portainer_data >/dev/null
docker rm -f portainer >/dev/null 2>&1 || true
docker run -d --name portainer --restart=always -p 8000:8000 -p 9443:9443 \
  -v /var/run/docker.sock:/var/run/docker.sock -v portainer_data:/data portainer/portainer-ce:lts
info "Portainer: https://${HOST}:9443"
info "Beim ersten Aufruf legst du das Admin-Konto an. Portainer sperrt die Einrichtung nach 5 Minuten –"
info "dann einmal 'docker restart portainer' ausführen und die Seite neu laden."
log "Fertig."
