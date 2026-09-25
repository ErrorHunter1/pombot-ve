#!/usr/bin/env bash
# PomBot Agent Installer – Debian 12/13, Ubuntu 22.04/24.04
#
#   sudo bash install-agent.sh [Optionen]
#
#   --source DIR         Verzeichnis mit dem Agent-Quellcode (Standard: Ordner dieses Skripts)
#   --port PORT          Port des Agents (Standard: 8007)
#   --allow CIDRS        Nur diese IPs/Netze dürfen den Agent ansprechen (Komma-getrennt, z. B. Panel-IP)
#   --local              Agent läuft auf demselben Server wie das Panel (Join-Code nutzt 127.0.0.1)
#   --create-bridge      Bridge (vmbr0) auf der Haupt-Netzwerkkarte anlegen (Netzwerk wird umgestellt!)
#   --bridge-name NAME   Name der Bridge (Standard: vmbr0)
#   --force              Auch auf nicht offiziell unterstützten Systemen installieren
#   --package            Aufruf aus dem .deb-Paket (Pakete und Dateien hat dpkg bereits installiert)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8007
ALLOW=""
LOCAL=0
CREATE_BRIDGE=0
BRIDGE=vmbr0
FORCE=0
PACKAGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --source) SRC="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --allow) ALLOW="$2"; shift 2 ;;
    --local) LOCAL=1; shift ;;
    --create-bridge) CREATE_BRIDGE=1; shift ;;
    --bridge-name) BRIDGE="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --package) PACKAGE=1; SRC=/opt/pombot/agent; shift ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done

log() { echo "==> $*"; }
die() { echo "FEHLER: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen."
[ -f "$SRC/pombot_agent/main.py" ] || die "Agent-Quellcode nicht gefunden in $SRC"

. /etc/os-release
case "${ID}:${VERSION_ID}" in
  debian:12|debian:13|ubuntu:22.04|ubuntu:24.04) log "System erkannt: $PRETTY_NAME" ;;
  *) [ "$FORCE" -eq 1 ] || [ "$PACKAGE" -eq 1 ] || die "Nicht unterstütztes System: $PRETTY_NAME (mit --force trotzdem installieren)" ;;
esac

if ! grep -Eq '(vmx|svm)' /proc/cpuinfo; then
  echo "WARNUNG: Keine Hardware-Virtualisierung (VT-x/AMD-V) gefunden. KVM-VMs laufen nur sehr langsam, LXC funktioniert."
fi

if [ "$PACKAGE" -eq 0 ]; then
log "Installiere Pakete …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients \
  lxc lxcfs uidmap wget gnupg xorriso e2fsprogs bridge-utils iproute2 nftables rclone nfs-common cifs-utils \
  python3 python3-venv openssl curl ca-certificates tar >/dev/null
apt-get install -y -qq lxc-templates >/dev/null 2>&1 || true
fi

systemctl enable --now libvirtd >/dev/null 2>&1 || systemctl enable --now libvirtd.service
systemctl enable --now lxcfs >/dev/null 2>&1 || true

log "Richte ID-Mapping für unprivilegierte Container ein …"
grep -q '^root:100000:65536' /etc/subuid || echo 'root:100000:65536' >> /etc/subuid
grep -q '^root:100000:65536' /etc/subgid || echo 'root:100000:65536' >> /etc/subgid

log "Kopiere Agent nach /opt/pombot/agent …"
mkdir -p /opt/pombot/agent /etc/pombot
if [ "$(readlink -f "$SRC")" != "/opt/pombot/agent" ]; then
  rm -rf /opt/pombot/agent/pombot_agent
  cp -r "$SRC/pombot_agent" "$SRC/requirements.txt" "$SRC/pombot-agent.service" "$SRC/pombot-network.service" \
        "$SRC/install-agent.sh" "$SRC/bridge-setup.sh" /opt/pombot/agent/
fi
[ -x /opt/pombot/agent/venv/bin/python ] || python3 -m venv /opt/pombot/agent/venv
/opt/pombot/agent/venv/bin/pip install -q --disable-pip-version-check --upgrade pip
/opt/pombot/agent/venv/bin/pip install -q --disable-pip-version-check -r /opt/pombot/agent/requirements.txt

mkdir -p /var/lib/pombot/{images,iso,guests,backups}
chmod 755 /var/lib/pombot /var/lib/pombot/guests /var/lib/pombot/iso

ENV=/etc/pombot/agent.env
if [ ! -f "$ENV" ]; then
  log "Erzeuge Konfiguration $ENV …"
  cat > "$ENV" <<EOF
POMBOT_AGENT_TOKEN=$(openssl rand -hex 32)
POMBOT_AGENT_BIND=0.0.0.0
POMBOT_AGENT_PORT=$PORT
POMBOT_AGENT_ALLOW=$ALLOW
POMBOT_LXC_BACKEND=loop
POMBOT_LXC_UNPRIVILEGED=1
EOF
  chmod 600 "$ENV"
else
  sed -i "s|^POMBOT_AGENT_PORT=.*|POMBOT_AGENT_PORT=$PORT|" "$ENV"
  if [ -n "$ALLOW" ]; then sed -i "s|^POMBOT_AGENT_ALLOW=.*|POMBOT_AGENT_ALLOW=$ALLOW|" "$ENV"; fi
fi
if [ "$LOCAL" -eq 1 ]; then sed -i "s|^POMBOT_AGENT_BIND=.*|POMBOT_AGENT_BIND=127.0.0.1|" "$ENV"; fi
# shellcheck disable=SC1090
. "$ENV"

MAIN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"
[ -n "$MAIN_IP" ] || MAIN_IP="$(hostname -I | awk '{print $1}')"

if [ ! -f /etc/pombot/agent-cert.pem ]; then
  log "Erzeuge TLS-Zertifikat …"
  SAN="DNS:$(hostname),DNS:localhost,IP:127.0.0.1"
  for ip in $(hostname -I); do SAN="$SAN,IP:$ip"; done
  openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
    -keyout /etc/pombot/agent-key.pem -out /etc/pombot/agent-cert.pem \
    -subj "/CN=$(hostname)" \
    -addext "subjectAltName=$SAN" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
    -addext "extendedKeyUsage=serverAuth" >/dev/null 2>&1
  chmod 600 /etc/pombot/agent-key.pem
fi

log "Starte Dienst pombot-agent …"
if [ "$PACKAGE" -eq 0 ]; then
  cp /opt/pombot/agent/pombot-agent.service /etc/systemd/system/pombot-agent.service
  cp /opt/pombot/agent/pombot-network.service /etc/systemd/system/pombot-network.service
fi
systemctl daemon-reload
systemctl enable pombot-network >/dev/null 2>&1
systemctl enable pombot-agent >/dev/null 2>&1
systemctl restart pombot-agent

for _ in $(seq 1 30); do
  if curl -sk "https://127.0.0.1:$POMBOT_AGENT_PORT/health" >/dev/null; then break; fi
  sleep 1
done
curl -sk "https://127.0.0.1:$POMBOT_AGENT_PORT/health" >/dev/null || die "Agent startet nicht – siehe: journalctl -u pombot-agent"

JOIN_HOST="$MAIN_IP"
[ "$LOCAL" -eq 1 ] && JOIN_HOST="127.0.0.1"
JOIN_JSON=$(python3 - "$JOIN_HOST" "$POMBOT_AGENT_PORT" "$POMBOT_AGENT_TOKEN" "$(hostname)" <<'PY'
import json, sys
cert = open("/etc/pombot/agent-cert.pem").read()
print(json.dumps({"host": sys.argv[1], "port": int(sys.argv[2]), "token": sys.argv[3], "name": sys.argv[4], "cert": cert}))
PY
)
JOIN_CODE=$(printf '%s' "$JOIN_JSON" | base64 -w0)

echo
echo "================================================================"
echo " PomBot Agent ist bereit auf $JOIN_HOST:$POMBOT_AGENT_PORT"
echo " Join-Code für das Panel (Nodes -> Node hinzufügen -> Join-Code):"
echo
echo "POMBOT_JOIN=$JOIN_CODE"
echo "================================================================"

if [ "$CREATE_BRIDGE" -eq 1 ]; then
  if [ -d "/sys/class/net/$BRIDGE" ]; then
    log "Bridge $BRIDGE existiert bereits – nichts zu tun."
  else
    log "Bridge $BRIDGE wird in wenigen Sekunden angelegt (Verbindung kann kurz abbrechen) …"
    nohup bash /opt/pombot/agent/bridge-setup.sh "$BRIDGE" >/var/log/pombot-bridge.log 2>&1 &
  fi
fi
