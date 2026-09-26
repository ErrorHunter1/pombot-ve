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
  enable_now docker
  docker compose version >/dev/null
}
in_container() { systemd-detect-virt --container -q 2>/dev/null; }
# Dienste aktivieren und starten. In unprivilegierten Containern dürfen Dienste keine eigenen Namespaces
# anlegen – Units mit Sandboxing (z. B. MariaDB, Redis) scheitern dann mit 226/NAMESPACE. In dem Fall wird die
# Sandbox des Dienstes per Drop-in gelockert und neu gestartet (wie beim Panel selbst in Containern).
enable_now() {
  local svc
  for svc in "$@"; do
    if systemctl enable --now "$svc"; then continue; fi
    if ! in_container; then journalctl -u "$svc" -n 30 --no-pager || true; return 1; fi
    log "Dienst $svc startet im Container nicht – lockere seine Sandbox …"
    mkdir -p "/etc/systemd/system/${svc%.service}.service.d"
    cat > "/etc/systemd/system/${svc%.service}.service.d/10-pombot-container.conf" <<'UNIT'
[Service]
PrivateTmp=no
PrivateDevices=no
PrivateUsers=no
ProtectSystem=no
ProtectHome=no
ProtectHostname=no
ProtectClock=no
ProtectKernelTunables=no
ProtectKernelModules=no
ProtectKernelLogs=no
ProtectControlGroups=no
ProtectProc=default
ProcSubset=all
RestrictNamespaces=no
PrivateMounts=no
PrivateIPC=no
ReadWritePaths=
ReadOnlyPaths=
InaccessiblePaths=
TemporaryFileSystem=
BindPaths=
BindReadOnlyPaths=
UNIT
    systemctl daemon-reload
    systemctl reset-failed "$svc" || true
    if ! systemctl restart "$svc"; then journalctl -u "$svc" -n 30 --no-pager || true; return 1; fi
    log "Dienst $svc läuft."
  done
}
php_fpm() { systemctl list-unit-files 'php*-fpm.service' --no-legend | awk '{print $1}' | sort -V | tail -n1; }
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
