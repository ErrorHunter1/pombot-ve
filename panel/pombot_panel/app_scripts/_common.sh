# ---- gemeinsame Hilfsfunktionen für alle Anwendungen (wird vor jedes Skript gesetzt) ----
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export HOME=/root
INFO=/root/pombot-app-info.txt
umask 022

log() { echo "==> $(date +%T) $*"; }
info() { echo "$*" >> "$INFO"; }
APT="apt-get -q -o DPkg::Lock::Timeout=900 -o Acquire::Retries=3"
apt_install() { $APT update; $APT install -y --no-install-recommends "$@"; }
genpw() { tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-20}" || true; }
install_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "Installiere Docker (get.docker.com) …"
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable --now docker
  docker compose version >/dev/null
}
public_ip() { curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'; }

: > "$INFO"
chmod 600 "$INFO"
log "Bereite System vor …"
command -v curl >/dev/null && command -v openssl >/dev/null || apt_install curl ca-certificates openssl
IP="$(public_ip)"
HOST="${APP_DOMAIN:-$IP}"
info "Anwendung: ${APP_NAME}"
info "Server: ${GUEST_HOSTNAME} (${IP})"
info ""
