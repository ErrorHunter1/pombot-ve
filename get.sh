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
#   --domain D   Panel unter dieser Domain mit Let's-Encrypt-Zertifikat (certbot) einrichten
#   --email E    E-Mail für Let's Encrypt (optional)
#   --no-domain  nicht nach einer Domain fragen
#
# Ohne --domain/--no-domain fragt der Installer interaktiv, ob eine Domain genutzt werden soll.
#
# Unterstützt: Debian 12/13, Ubuntu 22.04/24.04 (amd64)
set -euo pipefail

REPO="ErrorHunter1/pombot-ve"
MODE="all"
TAG=""
DOMAIN=""
EMAIL=""
ASK_DOMAIN=1
while [ $# -gt 0 ]; do
  case "$1" in
    --panel) MODE="panel"; shift ;;
    --node|--agent) MODE="node"; shift ;;
    --all) MODE="all"; shift ;;
    --version) TAG="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; ASK_DOMAIN=0; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --no-domain) ASK_DOMAIN=0; shift ;;
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

# ------------------------------------------------------------------ Domain + certbot
ask() {  # ask "Frage" Variable – liest vom Terminal, auch wenn das Skript per Pipe läuft
  local answer=""
  if (exec </dev/tty) 2>/dev/null; then
    read -r -p "$1" answer </dev/tty || true
  fi
  printf -v "$2" '%s' "$answer"
}

setup_domain() {
  local domain="$1" email="$2" public_ip dns_ip
  domain="$(echo "$domain" | tr 'A-Z' 'a-z' | sed 's/^https\?:\/\///; s/[/:].*$//')"
  if ! echo "$domain" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'; then
    warn "Ungültige Domain: $domain – überspringe."
    return 1
  fi
  public_ip="$(curl -fsS --max-time 5 https://1.1.1.1/cdn-cgi/trace 2>/dev/null | sed -n 's/^ip=//p')"
  dns_ip="$(getent ahostsv4 "$domain" 2>/dev/null | awk 'NR==1 {print $1}')"
  if [ -z "$dns_ip" ]; then
    warn "$domain hat (noch) keinen DNS-Eintrag. Lege einen A-Eintrag auf ${public_ip:-die IP dieses Servers} an."
    local go; ask "Trotzdem versuchen? [j/N] " go
    [[ "$go" =~ ^[jJyY]$ ]] || return 1
  elif [ -n "$public_ip" ] && [ "$dns_ip" != "$public_ip" ]; then
    warn "$domain zeigt auf $dns_ip, dieser Server hat aber $public_ip (evtl. Cloudflare-Proxy – dann erst auf „nur DNS“ stellen)."
    local go; ask "Trotzdem versuchen? [j/N] " go
    [[ "$go" =~ ^[jJyY]$ ]] || return 1
  fi
  if ss -ltnH '( sport = :80 )' | grep -q .; then
    warn "Port 80 ist belegt ($(ss -ltnpH '( sport = :80 )' | grep -o 'users:(("[^"]*' | cut -d'"' -f2 | head -1)). certbot braucht ihn kurz für die Bestätigung."
    warn "Alternative ohne Port 80: im Panel unter Einstellungen → Domain & HTTPS (per Cloudflare-DNS)."
    return 1
  fi
  log "Hole Let's-Encrypt-Zertifikat für $domain (certbot, Port 80 muss von außen erreichbar sein) …"
  apt-get install -y -qq certbot >/dev/null
  if ! certbot certonly --standalone -d "$domain" --non-interactive --agree-tos \
        ${email:+-m "$email"} ${email:---register-unsafely-without-email} --keep-until-expiring >/dev/null 2>"$TMP/certbot.err"; then
    tail -n 5 "$TMP/certbot.err" >&2
    warn "Zertifikat konnte nicht geholt werden – Panel bleibt unter der IP erreichbar."
    return 1
  fi
  # certbot legt Zertifikate nur für root lesbar ab: nach jeder Erneuerung ins Panel kopieren
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat > /etc/letsencrypt/renewal-hooks/deploy/pombot-panel.sh <<EOF
#!/bin/sh
# Von PomBot VE: erneuertes Zertifikat für das Panel übernehmen
[ -z "\$RENEWED_DOMAINS" ] || echo "\$RENEWED_DOMAINS" | grep -qw "$domain" || exit 0
# Wurde die Panel-Domain inzwischen im Panel geändert, nichts überschreiben
grep -q "^POMBOT_BASE_URL=https://$domain\$" /var/lib/pombot-panel/runtime.env 2>/dev/null || exit 0
install -d -o pombot -g pombot -m 750 /var/lib/pombot-panel/tls
install -o pombot -g pombot -m 644 /etc/letsencrypt/live/$domain/fullchain.pem /var/lib/pombot-panel/tls/fullchain.pem
install -o pombot -g pombot -m 600 /etc/letsencrypt/live/$domain/privkey.pem /var/lib/pombot-panel/tls/privkey.pem
systemctl restart pombot-panel
EOF
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/pombot-panel.sh
  cat > /var/lib/pombot-panel/runtime.env <<EOF
# Vom Installer gesetzt (Domain + certbot). Ändern: Panel → Einstellungen → Domain & HTTPS
POMBOT_BASE_URL=https://$domain
POMBOT_PORT=443
POMBOT_TLS_CERT=/var/lib/pombot-panel/tls/fullchain.pem
POMBOT_TLS_KEY=/var/lib/pombot-panel/tls/privkey.pem
EOF
  chown pombot:pombot /var/lib/pombot-panel/runtime.env
  RENEWED_DOMAINS="$domain" /etc/letsencrypt/renewal-hooks/deploy/pombot-panel.sh
  sleep 3
  if systemctl is-active --quiet pombot-panel; then
    PANEL_URL="https://$domain"
    log "Panel läuft unter $PANEL_URL (Zertifikat wird von certbot automatisch erneuert)."
  else
    warn "Panel startet mit der Domain nicht – zurück auf IP-Betrieb."
    rm -f /var/lib/pombot-panel/runtime.env
    systemctl restart pombot-panel
    return 1
  fi
}

PANEL_URL=""
if [ "$MODE" != "node" ]; then
  if [ "$ASK_DOMAIN" = 1 ] && [ -z "$DOMAIN" ]; then
    echo
    ask "Willst du eine Domain für dein Panel nutzen (z. B. panel.deinedomain.de)? [j/N] " WANT
    if [[ "${WANT:-}" =~ ^[jJyY]$ ]]; then
      ask "Domain: " DOMAIN
      ask "E-Mail für Let's Encrypt (optional, Enter = keine): " EMAIL
    fi
  fi
  if [ -n "$DOMAIN" ]; then
    setup_domain "$DOMAIN" "$EMAIL" || true
  fi
fi

echo
echo "================================================================"
echo " PomBot VE $TAG ist installiert."
if [ "$MODE" != "node" ]; then
  if [ -n "$PANEL_URL" ]; then
    echo " Panel: $PANEL_URL"
    grep -m1 "Admin angelegt" /root/pombot-admin.txt 2>/dev/null | sed 's/^/ /' || true
  elif [ -f /root/pombot-admin.txt ]; then
    sed 's/^/ /' /root/pombot-admin.txt
  else
    echo " Panel: siehe POMBOT_BASE_URL in /etc/pombot/panel.env (bestehende Zugangsdaten bleiben gültig)"
  fi
  echo
  if [ -z "$PANEL_URL" ]; then
    echo " Browser: https-Adresse oben öffnen, Zertifikatswarnung einmal bestätigen."
    echo " Eigene Domain mit echtem Zertifikat: Einstellungen → Domain & HTTPS"
  fi
fi
if [ "$MODE" = "node" ]; then
  echo " Diesen Node im Panel hinzufügen: Nodes → Node hinzufügen → Join-Code:"
  echo
  echo " $JOIN"
  echo
  echo " Tipp: Agent auf die Panel-IP beschränken:"
  echo "   sed -i 's/^POMBOT_AGENT_ALLOW=.*/POMBOT_AGENT_ALLOW=<Panel-IP>/' /etc/pombot/agent.env && systemctl restart pombot-agent"
fi
if [ -n "$PANEL_URL" ]; then
  echo " Firewall: 443/tcp (Panel) und 80/tcp (Zertifikats-Erneuerung) öffnen; Nodes: 8007/tcp nur für die Panel-IP."
else
  echo " Firewall: Panel-Port (Standard 8443/tcp) öffnen; Nodes brauchen 8007/tcp nur für die Panel-IP."
fi
echo "================================================================"
