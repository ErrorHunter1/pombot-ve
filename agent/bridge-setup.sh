#!/usr/bin/env bash
# Legt eine Linux-Bridge auf der Haupt-Netzwerkkarte an und übernimmt deren IP-Konfiguration.
# Unterstützt netplan (Ubuntu, neuere Debian-Cloud-Images) und ifupdown (klassisches Debian).
# Backups der alten Konfiguration liegen unter /etc/pombot/network-backup-<Zeit>/.
set -euo pipefail
BRIDGE="${1:-vmbr0}"
sleep 5

IFACE="$(ip route show default | awk '{print $5; exit}')"
[ -n "$IFACE" ] || { echo "Keine Default-Route gefunden"; exit 1; }
[ "$IFACE" != "$BRIDGE" ] || { echo "Bridge existiert schon"; exit 0; }
GW4="$(ip -4 route show default | awk '{print $3; exit}')"
ADDRS4="$(ip -o -4 addr show dev "$IFACE" scope global | awk '{print $4}')"
ADDRS6="$(ip -o -6 addr show dev "$IFACE" scope global | awk '{print $4}')"
GW6="$(ip -6 route show default 2>/dev/null | awk '{print $3; exit}')"
MAC="$(cat "/sys/class/net/$IFACE/address")"
DNS="$(awk '/^nameserver/ {print $2}' /etc/resolv.conf | grep -v '^127\.' | paste -sd, - || true)"
[ -n "$DNS" ] || DNS="1.1.1.1,8.8.8.8"
BACKUP="/etc/pombot/network-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
echo "Interface: $IFACE, IPv4: $ADDRS4, GW: $GW4, IPv6: $ADDRS6, GW6: $GW6, MAC: $MAC"

if ls /etc/netplan/*.yaml >/dev/null 2>&1; then
  cp -a /etc/netplan/. "$BACKUP/"
  ADDR_LIST=""
  for a in $ADDRS4 $ADDRS6; do ADDR_LIST="$ADDR_LIST\"$a\", "; done
  ROUTES=""
  [ -n "$GW4" ] && ROUTES="$ROUTES{\"to\": \"0.0.0.0/0\", \"via\": \"$GW4\", \"on-link\": true}, "
  [ -n "$GW6" ] && ROUTES="$ROUTES{\"to\": \"::/0\", \"via\": \"$GW6\", \"on-link\": true}, "
  DNS_LIST="$(echo "$DNS" | sed 's/[^,]*/"&"/g')"
  rm -f /etc/netplan/*.yaml
  cat > /etc/netplan/01-pombot-bridge.yaml <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    $IFACE:
      dhcp4: false
      dhcp6: false
  bridges:
    $BRIDGE:
      interfaces: [$IFACE]
      macaddress: "$MAC"
      addresses: [${ADDR_LIST%, }]
      routes: [${ROUTES%, }]
      nameservers:
        addresses: [$DNS_LIST]
      parameters:
        stp: false
        forward-delay: 0
EOF
  chmod 600 /etc/netplan/01-pombot-bridge.yaml
  netplan generate
  netplan apply
elif [ -f /etc/network/interfaces ]; then
  cp -a /etc/network/interfaces "$BACKUP/"
  {
    echo "# Von PomBot erzeugt – altes Backup: $BACKUP"
    echo "source /etc/network/interfaces.d/*"
    echo
    echo "auto lo"
    echo "iface lo inet loopback"
    echo
    echo "iface $IFACE inet manual"
    echo
    echo "auto $BRIDGE"
    first=1
    for a in $ADDRS4; do
      if [ $first -eq 1 ]; then
        echo "iface $BRIDGE inet static"
        echo "    address $a"
        [ -n "$GW4" ] && echo "    gateway $GW4"
        echo "    bridge-ports $IFACE"
        echo "    bridge-stp off"
        echo "    bridge-fd 0"
        echo "    hwaddress ether $MAC"
        first=0
      else
        echo "    up ip addr add $a dev $BRIDGE"
      fi
    done
    for a in $ADDRS6; do
      echo
      echo "iface $BRIDGE inet6 static"
      echo "    address $a"
      [ -n "$GW6" ] && echo "    gateway $GW6"
    done
  } > /etc/network/interfaces.new
  mv /etc/network/interfaces.new /etc/network/interfaces
  systemctl restart networking || { ifdown "$IFACE" || true; ifup "$BRIDGE"; }
else
  echo "Weder netplan noch ifupdown gefunden – bitte Bridge $BRIDGE manuell anlegen."
  exit 1
fi
echo "Bridge $BRIDGE angelegt."
