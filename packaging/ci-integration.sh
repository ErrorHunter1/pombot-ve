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

step "KVM-VM (Debian-12-Cloud-Image, verschachtelte Virtualisierung wie bei VPS-Hostern)"
if [ -e /dev/kvm ]; then
  sudo chmod 666 /dev/kvm || true
  KTPL="$(api GET /api/templates | json '[t["id"] for t in d if t["type"]=="kvm" and t["name"].startswith("Debian 12")][0]')"
  CIPOOL="$(api GET /api/pools | json '[p["id"] for p in d if p["name"]=="ci"][0]')"
  RES="$(api POST /api/guests "{\"template_id\":$KTPL,\"name\":\"ci-vm\",\"hostname\":\"ci-vm\",\"cores\":2,\"memory_mb\":1024,\"disk_gb\":8,\"ipv4_pool\":$CIPOOL,\"password\":\"CiTestPasswort1\"}")"
  echo "$RES"
  KGID="$(echo "$RES" | json 'd["guest_id"]')"
  KVMID="$(echo "$RES" | json 'd["vmid"]')"
  wait_task "$(echo "$RES" | json 'd["task_id"]')" 1500
  KIP="$(api GET "/api/guests/$KGID" | json 'd["ips"][0]["address"]')"
  sudo virsh dumpxml "pv$KVMID" | grep -qE "<serial|<console" && fail "VM hat noch eine serielle Konsole"
  echo "Warte, bis die VM gebootet ist und SSH auf $KIP antwortet (cloud-init) …"
  for i in $(seq 1 72); do
    if timeout 3 bash -c "echo > /dev/tcp/$KIP/22" 2>/dev/null; then echo "SSH erreichbar nach $((i * 5)) s"; break; fi
    sleep 5
  done
  if ! timeout 3 bash -c "echo > /dev/tcp/$KIP/22"; then
    set +e  # Diagnose soll vollständig durchlaufen, auch wenn einzelne Befehle scheitern
    mkdir -p /tmp/diag
    sudo virsh screenshot "pv$KVMID" /tmp/diag/vm-screen.png
    sudo cp /var/log/libvirt/qemu/pv$KVMID.log /tmp/diag/ 2>/dev/null || true
    sudo cp -r "/var/lib/pombot/guests/pv$KVMID/seed" /tmp/diag/seed 2>/dev/null || true
    sudo virsh dumpxml "pv$KVMID" > /tmp/diag/domain.xml
    { ping -c 2 -W 2 "$KIP"; ip neigh; bridge fdb show br vmbr0; bridge link; sudo virsh domiflist "pv$KVMID";
      sudo virsh domstats "pv$KVMID" --interface --cpu-total; sudo iptables -S FORWARD; ls -la /dev/kvm; } > /tmp/diag/net.txt 2>&1
    sudo chown -R "$(id -u):$(id -g)" /tmp/diag; chmod -R a+rX /tmp/diag
    cat /tmp/diag/net.txt
    fail "VM nicht per SSH erreichbar"
  fi
  sleep 20
  api GET "/api/guests/$KGID/status" | json 'd["state"], d["cpu"], d["memory_used"]'
  wait_task "$(api DELETE "/api/guests/$KGID" | json 'd["task_id"]')" 300
  sudo virsh list --all --name | grep -q "pv$KVMID" && fail "VM wurde nicht gelöscht"
else
  echo "::warning::/dev/kvm fehlt auf diesem Runner – KVM-Test übersprungen"
fi
end

step "Geroutete Zusatz-IPs: Erkennung und Pool mit Einzeladressen"
# Simuliert Zusatz-IPs wie bei skrime/Hetzner: eine davon ist direkt auf der Netzwerkkarte eingetragen
UPLINK="$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')"
sudo ip addr add 192.0.2.10/32 dev "$UPLINK"
sudo iptables -t nat -A POSTROUTING -s 192.0.2.0/24 -o "$UPLINK" -j MASQUERADE   # nur im CI nötig (Test-Adressen)
NODE_ID="$(api GET /api/nodes | json 'd[0]["id"]')"
api POST "/api/nodes/$NODE_ID/refresh" > /dev/null
api GET "/api/nodes/$NODE_ID/network" | json '[(a["address"], a["interface"], a["main"]) for a in d["addresses"]]' | tee /tmp/detect.txt
grep -q "192.0.2.10" /tmp/detect.txt || fail "Erkennung findet die Zusatz-IP nicht"
RPOOL="$(api POST /api/pools "{\"name\":\"zusatz\",\"mode\":\"routed\",\"node_id\":$NODE_ID,\"address_list\":\"192.0.2.10, 192.0.2.11\"}")"
echo "$RPOOL"
RPOOL_ID="$(echo "$RPOOL" | json 'd["id"]')"
end

step "Container im gerouteten Modus"
RES="$(api POST /api/guests "{\"template_id\":$TPL,\"name\":\"ci-routed\",\"hostname\":\"ci-routed\",\"cores\":1,\"memory_mb\":512,\"disk_gb\":4,\"ipv4_pool\":$RPOOL_ID,\"password\":\"CiTestPasswort1\"}")"
echo "$RES"
RGID="$(echo "$RES" | json 'd["guest_id"]')"
RVMID="$(echo "$RES" | json 'd["vmid"]')"
wait_task "$(echo "$RES" | json 'd["task_id"]')" 1200
ip -4 addr show dev "$UPLINK" | grep -q "192.0.2.10" && fail "Zusatz-IP wurde nicht von der Netzwerkkarte des Hosts genommen"
ip route show 192.0.2.10 | tee /dev/stderr | grep -q pbr0 || fail "Route zu 192.0.2.10 über pbr0 fehlt"
sysctl net.ipv4.ip_forward "net.ipv4.conf.$UPLINK.proxy_arp"
sudo lxc-attach -n "pv$RVMID" -- ip -4 addr show eth0 | grep -q "192.0.2.10/32" || fail "IP /32 nicht im Container"
sudo lxc-attach -n "pv$RVMID" -- ip route | tee /dev/stderr | grep -q "default via" || fail "Default-Route fehlt im Container"
ping -c 2 -W 2 192.0.2.10 || fail "Gerouteter Container nicht erreichbar"
# Azure (GitHub-Runner) blockiert ausgehendes ICMP – daher TCP-Verbindung statt Ping prüfen
sudo lxc-attach -n "pv$RVMID" -- timeout 10 bash -c 'echo > /dev/tcp/1.1.1.1/443' || fail "Gerouteter Container hat kein Internet"
echo "Internet aus dem gerouteten Container: OK (TCP 1.1.1.1:443)"
sudo lxc-attach -n "pv$RVMID" -- test -x /usr/sbin/sshd || fail "SSH im gerouteten Container nicht installiert"
sudo nft list chain bridge pombot input | grep -q "jump out_pv$RVMID" || fail "Spoofing-Schutz greift im gerouteten Modus nicht"
end

step "Geroutet: Löschen entfernt Route"
wait_task "$(api DELETE "/api/guests/$RGID" | json 'd["task_id"]')" 300
ip route show 192.0.2.10 | grep -q pbr0 && fail "Route wurde nicht entfernt"
api GET /api/pools | json '[(p["name"], p["used"], p["size"]) for p in d]'
end

echo "Integrationstest erfolgreich."
