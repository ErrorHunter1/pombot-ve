log "Installiere nginx …"
apt_install nginx
enable_now nginx
if [ -n "${APP_DOMAIN:-}" ]; then
  sed -i "s/server_name _;/server_name ${APP_DOMAIN} _;/" /etc/nginx/sites-available/default
  systemctl reload nginx
fi
info "Webserver: http://${HOST}/"
info "Dateien: /var/www/html"
info "Konfiguration: /etc/nginx/sites-available/default"
log "Fertig."
