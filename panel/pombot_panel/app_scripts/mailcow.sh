# mailcow: dockerized (docs.mailcow.email/getstarted/install/) ohne Rückfragen
apt_install git curl openssl jq
install_docker

log "Lade mailcow …"
cd /opt
if [ ! -d mailcow-dockerized ]; then
  git clone https://github.com/mailcow/mailcow-dockerized
fi
cd mailcow-dockerized

log "Erzeuge Konfiguration für ${APP_DOMAIN} …"
export MAILCOW_HOSTNAME="${APP_DOMAIN}"
export MAILCOW_TZ="${APP_TZ:-Europe/Berlin}"
export MAILCOW_BRANCH="master"
# Das Skript stellt Rückfragen nur, wenn Werte fehlen – der Rest wird mit Standardantworten bestätigt
yes "" | timeout 600 ./generate_config.sh || true
[ -f mailcow.conf ] || { echo "mailcow.conf wurde nicht erzeugt"; exit 1; }

log "Lade Container-Images (mehrere GB, dauert einige Minuten) …"
docker compose pull
log "Starte mailcow …"
docker compose up -d

info "mailcow: https://${APP_DOMAIN}"
info "Anmeldung (Admin): admin / moohoo  –  sofort ändern!"
info ""
info "DNS-Einträge (mindestens):"
info "  ${APP_DOMAIN}.  A     ${IP}"
info "  <deine-domain>.  MX 10 ${APP_DOMAIN}."
info "  <deine-domain>.  TXT   \"v=spf1 mx a -all\""
info "  autodiscover / autoconfig als CNAME auf ${APP_DOMAIN}"
info "Reverse-DNS (PTR) von ${IP} beim Hoster auf ${APP_DOMAIN} setzen, Port 25 muss ausgehend offen sein."
info "Dateien: /opt/mailcow-dockerized (Updates: ./update.sh)"
log "Fertig."
