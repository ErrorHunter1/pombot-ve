#!/usr/bin/env bash
# PomBot VE – Update im Hintergrund (läuft als root über pombot-update.service).
#
# Das Panel (Benutzer pombot) legt die gewünschte Version in /var/lib/pombot-panel/update/request ab.
# pombot-update.path startet daraufhin diesen Dienst. Das Skript lädt die .deb-Pakete aus den
# GitHub-Releases und installiert sie – das Panel und (falls vorhanden) der Agent auf diesem Server.
#
# Status und Log landen in /var/log (root-eigen), das Panel liest sie nur. So kann der Panel-Benutzer
# keine Datei beeinflussen, in die root schreibt.
set -uo pipefail

REQUEST=/var/lib/pombot-panel/update/request
STATUS=/var/log/pombot-update.json
LOG=/var/log/pombot-update.log
REPO="${POMBOT_UPDATE_REPO:-ErrorHunter1/pombot-ve}"
[ -r /etc/pombot/panel.env ] && REPO="$(sed -n 's/^POMBOT_UPDATE_REPO=//p' /etc/pombot/panel.env | tail -1)"
[ -n "$REPO" ] || REPO="ErrorHunter1/pombot-ve"

status() {  # status <state> <message> [tag]
  printf '{"state":"%s","message":"%s","tag":"%s","time":%s}\n' "$1" "$2" "${3:-${TAG:-}}" "$(date +%s)" > "$STATUS.tmp"
  chmod 644 "$STATUS.tmp"
  mv -f "$STATUS.tmp" "$STATUS"
}

# Auftrag lesen und sofort entfernen (sonst startet die Path-Unit erneut)
[ -f "$REQUEST" ] && [ ! -L "$REQUEST" ] || exit 0
TAG="$(head -c 64 "$REQUEST" | tr -d '[:space:]')"
rm -f "$REQUEST"

: > "$LOG"
chmod 644 "$LOG"
exec >>"$LOG" 2>&1
echo "$(date '+%F %T') Update auf $TAG angefordert (Quelle: github.com/$REPO)"

if ! [[ "$TAG" =~ ^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}$ ]]; then
  echo "Ungültige Version: $TAG"
  TAG=""  # ungeprüften Text nicht in die Status-Datei (JSON) schreiben
  status error "Ungültige Version"
  exit 1
fi
if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "Ungültiges Repository: $REPO"
  status error "Ungültiges Repository"
  exit 1
fi

VER="${TAG#v}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
status running "Lade Pakete herunter …"

PKGS=(pombot-panel)
dpkg -s pombot-agent >/dev/null 2>&1 && PKGS+=(pombot-agent)
FILES=()
for pkg in "${PKGS[@]}"; do
  url="https://github.com/$REPO/releases/download/$TAG/${pkg}_${VER}_all.deb"
  echo "Lade $url"
  if ! curl -fsSL --retry 3 -o "$TMP/${pkg}_${VER}_all.deb" "$url"; then
    echo "Download fehlgeschlagen: $url"
    status error "Download von $pkg fehlgeschlagen"
    exit 1
  fi
  FILES+=("$TMP/${pkg}_${VER}_all.deb")
done

status running "Installiere ${PKGS[*]} $TAG (Panel startet dabei neu) …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q || echo "WARNUNG: apt-get update fehlgeschlagen – versuche es trotzdem"
if apt-get install -y -q --allow-downgrades -o Dpkg::Options::=--force-confold "${FILES[@]}"; then
  echo "$(date '+%F %T') Update auf $TAG abgeschlossen."
  status ok "Update auf $TAG abgeschlossen"
else
  echo "$(date '+%F %T') Installation fehlgeschlagen."
  status error "Installation fehlgeschlagen – Details im Log"
  exit 1
fi
