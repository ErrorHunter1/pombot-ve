#!/usr/bin/env bash
# Integrationstest auf einem echten Linux-Host (GitHub Actions, Ubuntu):
# installiert beide .deb-Pakete, bindet den Host als Node ein, legt einen echten LXC-Container
# mit automatischer IP an und prüft Firewall, Backup, Zeitplan und Löschen.
set -euo pipefail

API=https://127.0.0.1:8443
JAR="$(mktemp)"
step() { echo; echo "::group::$*"; }
end() { echo "::endgroup::"; }
fail() { echo "::error::$*"; exit 1; }

api() {  # api METHOD PFAD [JSON]
  curl -sk -b "$JAR" -c "$JAR" -X "$1" -H "X-PomBot: 1" -H "Content-Type: application/json" \
    ${3:+-d "$3"} "$API$2"
}
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

wait_task() {  # wait_task ID TIMEOUT
  local id="$1" limit="${2:-900}" waited=0 status
  while [ "$waited" -lt "$limit" ]; do
    status="$(api GET "/api/tasks/$id" | json 'd["status"]')"
    [ "$status" != "running" ] && break
    sleep 5; waited=$((waited + 5))
  done
  api GET "/api/tasks/$id" | json 'd["log"]'
  [ "$status" = "ok" ] || fail "Aufgabe $id endete mit Status '$status'"
}

step "Pakete installieren"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q ./dist/pombot-panel_*.deb ./dist/pombot-agent_*.deb
systemctl is-active pombot-panel pombot-agent
end

step "Test-Bridge vmbr0 mit NAT anlegen"
sudo ip link add vmbr0 type bridge
sudo ip addr add 10.99.0.1/24 dev vmbr0
sudo ip link set vmbr0 up
sudo sysctl -qw net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -s 10.99.0.0/24 ! -d 10.99.0.0/24 -j MASQUERADE
sudo iptables -I FORWARD -i vmbr0 -j ACCEPT
sudo iptables -I FORWARD -o vmbr0 -j ACCEPT
end

step "Node registrieren"
JOIN="$(sudo bash /opt/pombot/agent/install-agent.sh --package | grep '^POMBOT_JOIN=' | tail -1)"
[ -n "$JOIN" ] || fail "Kein Join-Code"
sudo pombot-panel add-node --join "$JOIN" --name ci-node
end

step "Anmelden"
PW="$(sudo sed -n 's/^Admin angelegt: admin \/ //p' /root/pombot-admin.txt)"
[ -n "$PW" ] || fail "Admin-Passwort nicht gefunden"
api POST /api/auth/login "{\"username\":\"admin\",\"password\":\"$PW\"}" | grep -q '"ok":true' || fail "Login fehlgeschlagen"
api GET /api/nodes | json '[(n["name"], n["status"], n["info"]["bridges"]) for n in d]'
end

step "IP-Pool anlegen"
api POST /api/pools '{"name":"ci","network":"10.99.0.0/24","gateway":"10.99.0.1","dns":"1.1.1.1","bridge":"vmbr0","range_start":"10.99.0.10","range_end":"10.99.0.50"}' | json 'd["name"], d["size"]'
end

step "LXC-Container (Debian 12) erstellen"
TPL="$(api GET /api/templates | json '[t["id"] for t in d if t["type"]=="lxc" and t["lxc_release"]=="bookworm"][0]')"
RES="$(api POST /api/guests "{\"template_id\":$TPL,\"name\":\"ci-ct\",\"hostname\":\"ci-ct\",\"cores\":1,\"memory_mb\":512,\"disk_gb\":4,\"password\":\"CiTestPasswort1\"}")"
echo "$RES"
GID="$(echo "$RES" | json 'd["guest_id"]')"
TID="$(echo "$RES" | json 'd["task_id"]')"
wait_task "$TID" 1200
end

step "Container prüfen"
sudo lxc-ls -f
IP="$(api GET "/api/guests/$GID" | json 'd["ips"][0]["address"]')"
echo "Zugewiesene IP: $IP"
if ! sudo lxc-attach -n pv100 -- ip -4 addr show eth0 | grep -q "$IP"; then
  echo "--- Diagnose"
  sudo cat /var/lib/lxc/pv100/config
  sudo lxc-attach -n pv100 -- sh -c 'ip addr; ip route; ls -la /etc/netplan /etc/systemd/network /etc/network 2>&1; cat /etc/systemd/network/*.network /etc/network/interfaces 2>&1; systemctl is-enabled systemd-networkd networking 2>&1; networkctl status eth0 2>&1 | head -30'
  fail "IP $IP ist im Container nicht gesetzt"
fi
sudo lxc-attach -n pv100 -- hostname | grep -q ci-ct || fail "Hostname nicht gesetzt"
ping -c 2 -W 2 "$IP" || fail "Container nicht per Ping erreichbar"
if ! sudo lxc-attach -n pv100 -- test -x /usr/sbin/sshd; then
  echo "--- Diagnose Internet im Container"
  sudo lxc-attach -n pv100 -- sh -c 'ip route; cat /etc/resolv.conf; ls -la /etc/resolv.conf; ping -c1 -W2 1.1.1.1; getent hosts deb.debian.org' || true
  sudo iptables -S FORWARD; sudo iptables -t nat -S POSTROUTING; sysctl net.bridge.bridge-nf-call-iptables 2>/dev/null || true
  fail "SSH-Server nicht installiert"
fi
api GET "/api/guests/$GID/status" | json 'd["state"], d["cpu"], d["memory_used"]'
end

step "Firewall und Spoofing-Schutz"
sudo nft list table bridge pombot | tee /tmp/nft-before.txt
grep -Eq "ip saddr != (\{ )?$IP" /tmp/nft-before.txt || fail "Spoofing-Schutz fehlt"
api PUT "/api/guests/$GID/firewall" '{"enabled":true,"policy_in":"drop","policy_out":"accept","rules":[{"direction":"in","action":"accept","protocol":"tcp","port":"22,80,443"},{"direction":"in","action":"accept","protocol":"icmp"}]}' | json 'd["enabled"], len(d["rules"])'
sudo nft list table bridge pombot | tee /tmp/nft-after.txt
grep -q "chain in_pv100" /tmp/nft-after.txt || fail "Eingangs-Kette fehlt"
grep -q "tcp dport { 22, 80, 443 } accept" /tmp/nft-after.txt || fail "Portregel fehlt"
end

step "Aktionen: Neustart"
api POST "/api/guests/$GID/action" '{"action":"reboot"}'
sleep 10
sudo lxc-info -n pv100 -s
end

step "Backup und Zeitplan"
TID="$(api POST "/api/guests/$GID/backups" | json 'd["task_id"]')"
wait_task "$TID" 900
api GET "/api/guests/$GID/backups" | json '[(b["file"], b["size"]) for b in d]'
api PUT "/api/guests/$GID/backup-schedule" '{"enabled":true,"frequency":"daily","weekday":6,"hour":3,"minute":0,"keep":3}' | json 'd["next_run"]'
sudo lxc-info -n pv100 -s | grep -q RUNNING || fail "Container läuft nach dem Backup nicht wieder"
end

step "Snapshot"
TID="$(api POST "/api/guests/$GID/snapshots" '{"name":"ci-snap"}' | json 'd["task_id"]')"
wait_task "$TID" 900
api GET "/api/guests/$GID/snapshots" | json '[s["name"] for s in d]'
end

step "Löschen"
TID="$(api DELETE "/api/guests/$GID" | json 'd["task_id"]')"
wait_task "$TID" 300
sudo lxc-ls | grep -q pv100 && fail "Container existiert noch"
sudo nft list table bridge pombot | grep -q pv100 && fail "Firewall-Regeln wurden nicht entfernt"
api GET /api/pools | json '[(p["name"], p["used"]) for p in d]'
end

echo "Integrationstest erfolgreich."
