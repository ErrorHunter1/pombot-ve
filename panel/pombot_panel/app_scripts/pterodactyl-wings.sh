# Pterodactyl Wings (Daemon für Gameserver) nach pterodactyl.io/wings/1.0/installing.html
install_docker
log "Lade Wings …"
mkdir -p /etc/pterodactyl
ARCH="$([ "$(uname -m)" = "x86_64" ] && echo amd64 || echo arm64)"
curl -fsSL -o /usr/local/bin/wings "https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_${ARCH}"
chmod u+x /usr/local/bin/wings

cat > /etc/systemd/system/wings.service <<'EOF'
[Unit]
Description=Pterodactyl Wings Daemon
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
User=root
WorkingDirectory=/etc/pterodactyl
LimitNOFILE=4096
PIDFile=/var/run/wings/daemon.pid
ExecStart=/usr/local/bin/wings
Restart=on-failure
StartLimitInterval=180
StartLimitBurst=30
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable wings

if [ -n "${APP_PANEL_URL:-}" ] && [ -n "${APP_TOKEN:-}" ] && [ -n "${APP_NODE_ID:-}" ]; then
  log "Verbinde mit dem Pterodactyl Panel (Auto-Deploy) …"
  cd /etc/pterodactyl
  if wings configure --panel-url "${APP_PANEL_URL}" --token "${APP_TOKEN}" --node "${APP_NODE_ID}"; then
    systemctl restart wings
    info "Wings ist mit ${APP_PANEL_URL} (Node ${APP_NODE_ID}) verbunden und läuft."
  else
    info "HINWEIS: Automatische Verbindung fehlgeschlagen – Token/Node prüfen, dann erneut 'wings configure …' ausführen."
  fi
else
  info "Wings ist installiert, aber noch nicht mit einem Panel verbunden:"
  info "1. Im Pterodactyl Panel: Admin → Nodes → Node anlegen (FQDN/IP: ${HOST})"
  info "2. Reiter 'Configuration' → Inhalt nach /etc/pterodactyl/config.yml kopieren"
  info "   (oder 'Generate Token' und den angezeigten Befehl hier ausführen)"
  info "3. systemctl restart wings"
fi
info ""
info "Benötigte Ports: 8080 (Wings-API), 2022 (SFTP) und die Ports deiner Gameserver."
log "Fertig."
