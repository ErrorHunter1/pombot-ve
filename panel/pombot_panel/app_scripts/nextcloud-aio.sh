install_docker
log "Starte Nextcloud All-in-One …"
docker rm -f nextcloud-aio-mastercontainer >/dev/null 2>&1 || true
docker run --init --sig-proxy=false --name nextcloud-aio-mastercontainer --restart always \
  --publish 80:80 --publish 8080:8080 --publish 8443:8443 \
  --volume nextcloud_aio_mastercontainer:/mnt/docker-aio-config \
  --volume /var/run/docker.sock:/var/run/docker.sock:ro \
  -d nextcloud/all-in-one:latest
info "Einrichtung: https://${IP}:8080 (Zertifikatswarnung bestätigen)"
info "Dort steht das Passwort (Passphrase) für die Verwaltung. Danach die Domain eintragen –"
info "sie muss per DNS auf ${IP} zeigen, Ports 80, 443 und 8443 müssen offen sein."
[ -n "${APP_DOMAIN:-}" ] && info "Vorgesehene Domain: ${APP_DOMAIN}"
log "Fertig."
