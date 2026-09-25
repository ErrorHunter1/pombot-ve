#!/usr/bin/env bash
# PomBot VE – One-Click-Installer
#
#   curl -fsSL https://raw.githubusercontent.com/ErrorHunter1/pombot-ve/main/get.sh | sudo bash
#
# Optionen (nach `bash -s --` anhängen):
#   --panel      nur das Panel (Web-Oberfläche) installieren
#   --node       nur den Agent (Virtualisierungs-Host) installieren – Join-Code wird ausgegeben
#   (ohne)       Panel + Node auf diesem Server, fertig verbunden (Standard)
#   --version X  bestimmte Version statt der neuesten, z. B. --version v0.4.3
#
# Unterstützt: Debian 12/13, Ubuntu 22.04/24.04 (amd64)
set -euo pipefail

REPO="ErrorHunter1/pombot-ve"
MODE="all"
TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --panel) MODE="panel"; shift ;;
    --node|--agent) MODE="node"; shift ;;
    --all) MODE="all"; shift ;;
    --version) TAG="$2"; shift 2 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done

c_ok="\033[32m"; c_warn="\033[33m"; c_err="\033[31m"; c_off="\033[0m"
log() { printf '%b==>%b %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%bWARNUNG:%b %s\n' "$c_warn" "$c_off" "$*"; }
die() { printf '%bFEHLER:%b %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen: curl -fsSL … | sudo bash"
[ -r /etc/os-release ] || die "Unbekanntes Betriebssystem"
. /etc/os-release
case "${ID}:${VERSION_ID}" in
  debian:12|debian:13|ubuntu:22.04|ubuntu:24.04) log "System: $PRETTY_NAME" ;;
  *) die "Nicht unterstützt: $PRETTY_NAME (benötigt Debian 12/13 oder Ubuntu 22.04/24.04)" ;;
esac
[ "$(dpkg --print-architecture)" = "amd64" ] || die "Nur amd64 (x86_64) wird unterstützt"

# In Containern (LXC/OpenVZ) können keine VMs oder Container laufen – dort nur das Panel
VIRT="$(systemd-detect-virt --container 2>/dev/null || true)"
if [ -n "$VIRT" ] && [ "$VIRT" != "none" ] && [ "$MODE" != "panel" ]; then
  if [ "$MODE" = "node" ]; then
    die "Dieser Server ist ein Container ($VIRT) – als Node braucht es einen echten Server oder eine VM."
  fi
  warn "Dieser Server ist ein Container ($VIRT) – installiere nur das Panel (ohne Node)."
  MODE="panel"
fi

export DEBIAN_FRONTEND=noninteractive
log "Bereite Installation vor …"
apt-get update -qq
apt-get install -y -qq curl ca-certificates >/dev/null

if [ -z "$TAG" ]; then
  TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
  [ -n "$TAG" ] || die "Neueste Version konnte nicht ermittelt werden (GitHub erreichbar?)"
fi
VER="${TAG#v}"
log "Installiere PomBot VE $TAG ($([ "$MODE" = all ] && echo "Panel + Node" || ([ "$MODE" = panel ] && echo "nur Panel" || echo "nur Node")))"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fetch() {
  local pkg="$1"
  curl -fsSL -o "$TMP/${pkg}_${VER}_all.deb" \
    "https://github.com/$REPO/releases/download/$TAG/${pkg}_${VER}_all.deb" || die "Download von $pkg $TAG fehlgeschlagen"
}
PKGS=()
if [ "$MODE" != "node" ]; then fetch pombot-panel; PKGS+=("$TMP/pombot-panel_${VER}_all.deb"); fi
if [ "$MODE" != "panel" ]; then fetch pombot-agent; PKGS+=("$TMP/pombot-agent_${VER}_all.deb"); fi

log "Installiere Pakete (lädt Abhängigkeiten, dauert einige Minuten) …"
if ! apt-get install -y -q "${PKGS[@]}" > "$TMP/install.log" 2>&1; then
  tail -n 40 "$TMP/install.log" >&2
  die "Installation fehlgeschlagen (siehe Ausgabe oben)"
fi
grep -E "^==>" "$TMP/install.log" || true

JOIN="$(grep '^POMBOT_JOIN=' "$TMP/install.log" | tail -1 || true)"
if [ "$MODE" = "all" ]; then
  log "Verbinde diesen Server als Node mit dem Panel …"
  # Agent nur lokal erreichbar machen – Panel und Node laufen auf derselben Maschine
  JOIN="$(bash /opt/pombot/agent/install-agent.sh --package --local --allow 127.0.0.1/32 2>/dev/null | grep '^POMBOT_JOIN=' | tail -1 || true)"
  if [ -n "$JOIN" ] && pombot-panel add-node --join "$JOIN" --name "$(hostname)" >/dev/null 2>&1; then
    log "Node $(hostname) ist verbunden."
  else
    warn "Node konnte nicht automatisch verbunden werden – im Panel unter Nodes → Node hinzufügen nachholen."
  fi
fi

echo
echo "================================================================"
echo " PomBot VE $TAG ist installiert."
if [ "$MODE" != "node" ]; then
  if [ -f /root/pombot-admin.txt ]; then
    sed 's/^/ /' /root/pombot-admin.txt
  else
    echo " Panel: siehe POMBOT_BASE_URL in /etc/pombot/panel.env (bestehende Zugangsdaten bleiben gültig)"
  fi
  echo
  echo " Browser: https-Adresse oben öffnen, Zertifikatswarnung einmal bestätigen."
  echo " Eigene Domain mit echtem Zertifikat: Einstellungen → Domain & HTTPS"
fi
if [ "$MODE" = "node" ]; then
  echo " Diesen Node im Panel hinzufügen: Nodes → Node hinzufügen → Join-Code:"
  echo
  echo " $JOIN"
  echo
  echo " Tipp: Agent auf die Panel-IP beschränken:"
  echo "   sed -i 's/^POMBOT_AGENT_ALLOW=.*/POMBOT_AGENT_ALLOW=<Panel-IP>/' /etc/pombot/agent.env && systemctl restart pombot-agent"
fi
echo " Firewall: Panel-Port (Standard 8443/tcp) öffnen; Nodes brauchen 8007/tcp nur für die Panel-IP."
echo "================================================================"
