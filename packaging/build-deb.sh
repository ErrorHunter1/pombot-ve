#!/usr/bin/env bash
# Baut die installierbaren Pakete nach dist/:
#   pombot-panel_<version>_all.deb   – Web-Panel
#   pombot-agent_<version>_all.deb   – Virtualisierungs-Host (KVM, LXC, nftables)
# Benötigt: dpkg-deb (Debian/Ubuntu, WSL oder GitHub Actions)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
OUT="$ROOT/dist"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

# Versionsnummern im Code müssen zur Datei VERSION passen
grep -q "version = \"$VERSION\"" "$ROOT/panel/pombot_panel/config.py" || { echo "Panel-Version passt nicht zu VERSION ($VERSION)"; exit 1; }
grep -q "VERSION = \"$VERSION\"" "$ROOT/agent/pombot_agent/config.py" || { echo "Agent-Version passt nicht zu VERSION ($VERSION)"; exit 1; }

mkdir -p "$OUT"
MAINTAINER="PomBot VE <noreply@users.noreply.github.com>"

copy_clean() {  # copy_clean <quelle> <ziel>
  mkdir -p "$(dirname "$2")"
  cp -r "$1" "$2"
  find "$2" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "$2" -name '*.pyc' -delete 2>/dev/null || true
}

finish() {  # finish <paketverzeichnis> <name>
  local dir="$1" name="$2"
  find "$dir" -type d -exec chmod 755 {} +
  find "$dir" -type f -exec chmod 644 {} +
  chmod 755 "$dir"/DEBIAN/{postinst,prerm,postrm}
  find "$dir/opt" -name '*.sh' -exec chmod 755 {} +
  [ -d "$dir/usr/bin" ] && chmod 755 "$dir"/usr/bin/*
  local size
  size="$(du -sk --exclude=DEBIAN "$dir" | cut -f1)"
  sed -i "s/^Installed-Size:.*/Installed-Size: $size/" "$dir/DEBIAN/control"
  dpkg-deb --root-owner-group -Zxz --build "$dir" "$OUT/${name}_${VERSION}_all.deb" >/dev/null
  echo "Gebaut: dist/${name}_${VERSION}_all.deb"
}

# ------------------------------------------------------------------ pombot-agent
A="$BUILD/pombot-agent"
mkdir -p "$A/DEBIAN" "$A/opt/pombot/agent" "$A/usr/lib/systemd/system"
copy_clean "$ROOT/agent/pombot_agent" "$A/opt/pombot/agent/pombot_agent"
cp "$ROOT/agent/requirements.txt" "$ROOT/agent/install-agent.sh" "$ROOT/agent/bridge-setup.sh" "$A/opt/pombot/agent/"
cp "$ROOT/agent/pombot-agent.service" "$A/opt/pombot/agent/"
cp "$ROOT/agent/pombot-agent.service" "$A/usr/lib/systemd/system/pombot-agent.service"
cp "$ROOT/agent/pombot-network.service" "$A/opt/pombot/agent/"
cp "$ROOT/agent/pombot-network.service" "$A/usr/lib/systemd/system/pombot-network.service"

cat > "$A/DEBIAN/control" <<EOF
Package: pombot-agent
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: 0
Depends: python3 (>= 3.10), python3-venv, openssl, curl, ca-certificates, qemu-system-x86, qemu-utils, libvirt-daemon-system, libvirt-clients, lxc, uidmap, wget, gnupg, xorriso, e2fsprogs, bridge-utils, iproute2, nftables, tar
Recommends: lxcfs, lxc-templates
Section: admin
Priority: optional
Homepage: https://github.com/ErrorHunter1/pombot-ve
Description: PomBot VE Agent - Virtualisierungs-Host
 Verwaltet KVM-VMs (libvirt/QEMU) und LXC-Container, Firewall (nftables),
 Snapshots, Backups und Konsolen im Auftrag des PomBot-VE-Panels.
 Nach der Installation wird ein Join-Code ausgegeben, mit dem der Host
 im Panel hinzugefügt wird.
EOF

cat > "$A/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
  bash /opt/pombot/agent/install-agent.sh --package
  if ! grep -q '^POMBOT_AGENT_ALLOW=.\+' /etc/pombot/agent.env; then
    echo "HINWEIS: Beschränke den Agent auf die IP deines Panels:"
    echo "  sed -i 's/^POMBOT_AGENT_ALLOW=.*/POMBOT_AGENT_ALLOW=<Panel-IP>/' /etc/pombot/agent.env && systemctl restart pombot-agent"
  fi
fi
exit 0
EOF

cat > "$A/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "deconfigure" ]; then
  systemctl stop pombot-agent >/dev/null 2>&1 || true
  systemctl disable pombot-agent pombot-network >/dev/null 2>&1 || true
fi
exit 0
EOF

cat > "$A/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  rm -rf /opt/pombot/agent/venv
  find /opt/pombot/agent -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
if [ "$1" = "purge" ]; then
  rm -f /etc/pombot/agent.env /etc/pombot/agent-key.pem /etc/pombot/agent-cert.pem
  echo "Hinweis: VM-/Container-Daten unter /var/lib/pombot und /var/lib/lxc wurden NICHT gelöscht."
fi
exit 0
EOF
finish "$A" pombot-agent

# ------------------------------------------------------------------ pombot-panel
P="$BUILD/pombot-panel"
mkdir -p "$P/DEBIAN" "$P/opt/pombot/panel" "$P/usr/lib/systemd/system" "$P/usr/bin"
copy_clean "$ROOT/panel/pombot_panel" "$P/opt/pombot/panel/pombot_panel"
cp "$ROOT/panel/requirements.txt" "$ROOT/panel/setup-panel.sh" "$P/opt/pombot/panel/"
copy_clean "$ROOT/agent" "$P/opt/pombot/agent-src"
cp "$ROOT/panel/pombot-panel.service" "$P/usr/lib/systemd/system/pombot-panel.service"
cp "$ROOT/panel/pombot-panel-cli" "$P/usr/bin/pombot-panel"

cat > "$P/DEBIAN/control" <<EOF
Package: pombot-panel
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: 0
Depends: python3 (>= 3.10), python3-venv, openssl, curl, ca-certificates, iproute2, util-linux, passwd
Suggests: pombot-agent
Section: admin
Priority: optional
Homepage: https://github.com/ErrorHunter1/pombot-ve
Description: PomBot VE Panel - Web-Oberfläche für Virtualisierung
 Web-Panel zum Verwalten von KVM-VMs und LXC-Containern auf mehreren Hosts:
 automatische IP-Vergabe aus Pools, Discord-Login, Benutzer mit Kontingenten,
 Firewall pro Server, zeitgesteuerte Backups, Snapshots und Browser-Konsole.
EOF

cat > "$P/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
  find /opt/pombot -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  bash /opt/pombot/panel/setup-panel.sh
fi
exit 0
EOF

cat > "$P/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "deconfigure" ]; then
  systemctl stop pombot-panel >/dev/null 2>&1 || true
  systemctl disable pombot-panel >/dev/null 2>&1 || true
fi
exit 0
EOF

cat > "$P/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  rm -rf /opt/pombot/panel/venv
  find /opt/pombot -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
if [ "$1" = "purge" ]; then
  rm -f /etc/pombot/panel.env /etc/pombot/panel-key.pem /etc/pombot/panel-cert.pem
  rm -rf /var/lib/pombot-panel
  userdel pombot >/dev/null 2>&1 || true
fi
exit 0
EOF
finish "$P" pombot-panel
