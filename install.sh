#!/usr/bin/env bash
# PomBot VE – Installer aus dem Quellcode (Debian 12/13, Ubuntu 22.04/24.04)
# Alternative: die fertigen .deb-Pakete aus den GitHub-Releases (siehe README).
#
#   sudo bash install.sh [Optionen]
#
#   --with-agent          Diesen Server zusätzlich als Node (Virtualisierungs-Host) einrichten
#   --create-bridge       (mit --with-agent) Bridge vmbr0 auf der Haupt-Netzwerkkarte anlegen
#   --port PORT           HTTPS-Port des Panels (Standard: 8443)
#   --url URL             Öffentliche Adresse, z. B. https://panel.example.com:8443
#                         (wichtig für den Discord-Login; Standard: https://<Server-IP>:<Port>)
#   --admin NAME          Name des ersten Administrators (Standard: admin)
#   --force               Auch auf nicht offiziell unterstützten Systemen installieren
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_AGENT=0
CREATE_BRIDGE=0
SETUP_ARGS=()
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --with-agent) WITH_AGENT=1; shift ;;
    --create-bridge) CREATE_BRIDGE=1; shift ;;
    --port|--url|--admin) SETUP_ARGS+=("$1" "$2"); shift 2 ;;
    --force) FORCE=1; shift ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done

log() { echo "==> $*"; }
die() { echo "FEHLER: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen (sudo bash install.sh)."
. /etc/os-release
case "${ID}:${VERSION_ID}" in
  debian:12|debian:13|ubuntu:22.04|ubuntu:24.04) log "System erkannt: $PRETTY_NAME" ;;
  *) [ "$FORCE" -eq 1 ] || die "Nicht unterstütztes System: $PRETTY_NAME (mit --force trotzdem installieren)" ;;
esac

log "Installiere Pakete …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv openssl curl ca-certificates iproute2 >/dev/null

log "Kopiere Dateien nach /opt/pombot …"
rm -rf /opt/pombot/panel/pombot_panel /opt/pombot/agent-src
mkdir -p /opt/pombot/panel
cp -r "$HERE/panel/pombot_panel" "$HERE/panel/requirements.txt" "$HERE/panel/setup-panel.sh" \
  "$HERE/panel/pombot-update.sh" "$HERE/panel/pombot-update.service" "$HERE/panel/pombot-update.path" /opt/pombot/panel/
cp -r "$HERE/agent" /opt/pombot/agent-src
find /opt/pombot -name __pycache__ -prune -exec rm -rf {} +
install -m 644 "$HERE/panel/pombot-panel.service" /etc/systemd/system/pombot-panel.service
install -m 755 "$HERE/panel/pombot-panel-cli" /usr/local/bin/pombot-panel

bash /opt/pombot/panel/setup-panel.sh "${SETUP_ARGS[@]}"

if [ "$WITH_AGENT" -eq 1 ]; then
  log "Richte diesen Server zusätzlich als Node ein …"
  BRIDGE_FLAG=""
  [ "$CREATE_BRIDGE" -eq 1 ] && BRIDGE_FLAG="--create-bridge"
  OUT="$(bash /opt/pombot/agent-src/install-agent.sh --source /opt/pombot/agent-src --local --allow 127.0.0.1/32 $BRIDGE_FLAG | tee /dev/stderr)"
  JOIN="$(echo "$OUT" | grep '^POMBOT_JOIN=' | tail -1)"
  if [ -n "$JOIN" ] && pombot-panel add-node --join "$JOIN" --name "$(hostname)"; then
    log "Node $(hostname) hinzugefügt."
  else
    echo "WARNUNG: Node konnte nicht automatisch hinzugefügt werden."
  fi
fi

. /etc/pombot/panel.env
echo
echo "Discord-Login einrichten:"
echo "  1. https://discord.com/developers/applications -> Neue Anwendung -> OAuth2"
echo "  2. Redirect hinzufügen: $POMBOT_BASE_URL/api/auth/discord/callback"
echo "  3. Client-ID und -Secret in /etc/pombot/panel.env eintragen, dann: systemctl restart pombot-panel"
echo "Firewall: Port $POMBOT_PORT/tcp öffnen. Auf Nodes Port 8007/tcp nur für die Panel-IP freigeben."
