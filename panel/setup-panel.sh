#!/usr/bin/env bash
# Richtet das bereits nach /opt/pombot/panel kopierte Panel ein (Benutzer, Python-Umgebung,
# Konfiguration, Zertifikat, Dienst, erster Admin). Wird von install.sh und vom .deb-Paket genutzt.
# Mehrfaches Ausführen ist unkritisch: vorhandene Konfiguration und Daten bleiben erhalten.
#
#   setup-panel.sh [--port 8443] [--url https://…] [--admin admin]
set -euo pipefail

PORT=8443
URL=""
ADMIN=admin
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --admin) ADMIN="$2"; shift 2 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done
log() { echo "==> $*"; }

id pombot >/dev/null 2>&1 || useradd --system --home-dir /var/lib/pombot-panel --shell /usr/sbin/nologin pombot
mkdir -p /etc/pombot /var/lib/pombot-panel
chown pombot:pombot /var/lib/pombot-panel
chmod 750 /var/lib/pombot-panel

log "Richte Python-Umgebung ein (lädt Pakete aus dem Internet) …"
[ -x /opt/pombot/panel/venv/bin/python ] || python3 -m venv /opt/pombot/panel/venv
/opt/pombot/panel/venv/bin/pip install -q --disable-pip-version-check --upgrade pip
/opt/pombot/panel/venv/bin/pip install -q --disable-pip-version-check -r /opt/pombot/panel/requirements.txt

MAIN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"
[ -n "$MAIN_IP" ] || MAIN_IP="$(hostname -I | awk '{print $1}')"
[ -n "$URL" ] || URL="https://$MAIN_IP:$PORT"

ENV=/etc/pombot/panel.env
if [ ! -f "$ENV" ]; then
  log "Erzeuge Konfiguration $ENV …"
  cat > "$ENV" <<EOF
# ---- PomBot Panel ----
POMBOT_SECRET_KEY=$(openssl rand -hex 32)
POMBOT_DB_URL=sqlite:////var/lib/pombot-panel/panel.db
POMBOT_BASE_URL=$URL
POMBOT_BIND=0.0.0.0
POMBOT_PORT=$PORT
POMBOT_AGENT_SRC=/opt/pombot/agent-src

# ---- Discord-Login (https://discord.com/developers/applications) ----
# Redirect-URL in der Discord-App eintragen: $URL/api/auth/discord/callback
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
# Optional: nur Mitglieder dieses Discord-Servers (Guild-ID) dürfen sich anmelden
DISCORD_GUILD_ID=
# Discord-User-IDs, die automatisch Administrator werden (Komma-getrennt)
DISCORD_ADMIN_IDS=
# open = jeder darf rein | approval = Admin muss freischalten | closed = nur bestehende Konten
POMBOT_REGISTRATION=approval

# ---- Standard-Kontingent für neue Benutzer ----
POMBOT_QUOTA_GUESTS=2
POMBOT_QUOTA_CORES=4
POMBOT_QUOTA_MEMORY_MB=4096
POMBOT_QUOTA_DISK_GB=50
POMBOT_QUOTA_IPS=2
POMBOT_DEFAULT_DNS=1.1.1.1,8.8.8.8

# ---- Sicherheit & Backups ----
# Server dürfen nur mit ihren eigenen IPs senden (Schutz vor IP-Spoofing)
POMBOT_ANTISPOOF=1
# Maximal aufbewahrte automatische Backups pro Server für normale Benutzer
POMBOT_MAX_AUTO_BACKUPS=7
EOF
fi
chown root:pombot "$ENV"
chmod 640 "$ENV"

if [ ! -f /etc/pombot/panel-cert.pem ]; then
  log "Erzeuge selbstsigniertes TLS-Zertifikat (später durch ein echtes ersetzbar) …"
  openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
    -keyout /etc/pombot/panel-key.pem -out /etc/pombot/panel-cert.pem \
    -subj "/CN=$(hostname)" -addext "subjectAltName=DNS:$(hostname),IP:$MAIN_IP,IP:127.0.0.1" >/dev/null 2>&1
fi
chown root:pombot /etc/pombot/panel-key.pem /etc/pombot/panel-cert.pem
chmod 640 /etc/pombot/panel-key.pem

# In Containern (LXC, OpenVZ, Docker …) darf systemd keine eigenen Mount-Namespaces anlegen –
# ProtectSystem/PrivateTmp würden den Start mit Fehler 226/NAMESPACE verhindern.
DROPIN=/etc/systemd/system/pombot-panel.service.d/10-container.conf
VIRT="$(systemd-detect-virt --container 2>/dev/null || true)"
if [ -n "$VIRT" ] && [ "$VIRT" != "none" ]; then
  log "Container erkannt ($VIRT) – passe Dienst-Absicherung an …"
  mkdir -p "$(dirname "$DROPIN")"
  cat > "$DROPIN" <<'EOF'
[Service]
ProtectSystem=no
PrivateTmp=no
EOF
else
  rm -f "$DROPIN"
fi

log "Starte Dienst pombot-panel …"
systemctl daemon-reload
systemctl enable pombot-panel >/dev/null 2>&1
systemctl restart pombot-panel

# Ersten Administrator nur anlegen, wenn es noch keinen Benutzer gibt
if [ -z "$(pombot-panel list-users 2>/dev/null)" ]; then
  INFO="$(pombot-panel create-admin --username "$ADMIN")"
  umask 077
  printf '%s\nPanel: %s\n' "$INFO" "$URL" > /root/pombot-admin.txt
  echo
  echo "================================================================"
  echo " PomBot VE Panel: $URL"
  echo " $INFO"
  echo " (auch gespeichert in /root/pombot-admin.txt)"
  echo "================================================================"
else
  echo "Panel aktualisiert: $URL"
fi
