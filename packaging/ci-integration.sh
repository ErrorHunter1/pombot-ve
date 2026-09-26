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
api GET "/api/guests/$GID/backups" | json '[(b["file"], b["size"], b["location_name"]) for b in d["items"]]'
api PUT "/api/guests/$GID/backup-schedule" '{"enabled":true,"frequency":"daily","weekday":6,"hour":3,"minute":0,"keep":3}' | json 'd["next_run"]'
sudo lxc-info -n pv100 -s | grep -q RUNNING || fail "Container läuft nach dem Backup nicht wieder"
end

step "Snapshot"
TID="$(api POST "/api/guests/$GID/snapshots" '{"name":"ci-snap"}' | json 'd["task_id"]')"
wait_task "$TID" 900
api GET "/api/guests/$GID/snapshots" | json '[s["name"] for s in d]'
end

step "Externe Backup-Speicher bereitstellen (SFTP, S3/MinIO, NFS, SMB)"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-kernel-server samba >/dev/null
# SFTP: lokaler Benutzer mit Passwort
sudo useradd -m -s /bin/bash pbbackup && echo 'pbbackup:SftpTest12345' | sudo chpasswd
printf 'PasswordAuthentication yes\nKbdInteractiveAuthentication yes\n' | sudo tee /etc/ssh/sshd_config.d/00-pbtest.conf >/dev/null
sudo systemctl restart ssh || sudo systemctl start ssh
# S3: moto als S3-kompatibler Testserver (verhält sich wie AWS S3)
python3 -m venv /tmp/s3venv && /tmp/s3venv/bin/pip install -q "moto[server]"
nohup /tmp/s3venv/bin/moto_server -H 127.0.0.1 -p 9000 >/tmp/moto.log 2>&1 &
# NFS
sudo mkdir -p /srv/nfsbackup
echo "/srv/nfsbackup 127.0.0.1(rw,sync,no_root_squash,no_subtree_check)" | sudo tee -a /etc/exports >/dev/null
sudo exportfs -ra && sudo systemctl restart nfs-kernel-server
# SMB
sudo useradd -M pbsmb && sudo mkdir -p /srv/smbbackup && sudo chown pbsmb /srv/smbbackup
(echo SmbTest12345; echo SmbTest12345) | sudo smbpasswd -a -s pbsmb
printf '[pbbackup]\n  path = /srv/smbbackup\n  writable = yes\n  valid users = pbsmb\n' | sudo tee -a /etc/samba/smb.conf >/dev/null
sudo systemctl restart smbd
sleep 5
RCLONE_CONFIG_M_TYPE=s3 RCLONE_CONFIG_M_PROVIDER=Other RCLONE_CONFIG_M_ENDPOINT=http://127.0.0.1:9000 \
  RCLONE_CONFIG_M_ACCESS_KEY_ID=pbminio RCLONE_CONFIG_M_SECRET_ACCESS_KEY=pbminio12345 rclone mkdir m:pombot-ci
end

for KIND in sftp s3 nfs smb; do
  step "Backup auf $KIND: Test, Sichern, Liste, Wiederherstellen, Löschen"
  case "$KIND" in
    sftp) CFG='{"host":"127.0.0.1","port":22,"user":"pbbackup","password":"SftpTest12345","path":"backups"}' ;;
    s3)   CFG='{"provider":"Other","endpoint":"http://127.0.0.1:9000","region":"us-east-1","bucket":"pombot-ci","access_key":"pbminio","secret_key":"pbminio12345","path":"pb"}' ;;
    nfs)  CFG='{"server":"127.0.0.1","export":"/srv/nfsbackup","options":"vers=4,soft","path":"pb"}' ;;
    smb)  CFG='{"share":"//127.0.0.1/pbbackup","user":"pbsmb","password":"SmbTest12345","version":"3.0","path":"pb"}' ;;
  esac
  TGT="$(api POST /api/admin/backup-targets "{\"name\":\"ci-$KIND\",\"type\":\"$KIND\",\"config\":$CFG}" | json 'd["id"]')"
  api POST "/api/admin/backup-targets/$TGT/test" | tee /tmp/tgt-test.json
  json 'all(r["ok"] for r in d)' < /tmp/tgt-test.json | grep -q True || fail "Verbindungstest $KIND fehlgeschlagen"
  wait_task "$(api POST "/api/guests/$GID/backups" "{\"target_id\":$TGT}" | json 'd["task_id"]')" 900
  FILE="$(api GET "/api/guests/$GID/backups" | json "[b['file'] for b in d['items'] if b['location']==$TGT][0]")"
  [ -n "$FILE" ] || fail "Backup nicht auf $KIND gefunden"
  echo "Extern gespeichert: $FILE"
  api GET "/api/guests/$GID/backups" | json "[b for b in d['items'] if b['location'] is None and b['file']=='$FILE']" | grep -q "\[\]" || fail "Lokale Kopie hätte gelöscht werden sollen"
  wait_task "$(api POST "/api/guests/$GID/backups/$FILE/restore?target=$TGT" | json 'd["task_id"]')" 900
  sudo lxc-info -n pv100 -s | grep -q RUNNING || fail "Container läuft nach Wiederherstellung von $KIND nicht"
  api DELETE "/api/guests/$GID/backups/$FILE?target=$TGT" | grep -q ok || fail "Löschen auf $KIND fehlgeschlagen"
  end
done

step "Löschen"
TID="$(api DELETE "/api/guests/$GID" | json 'd["task_id"]')"
wait_task "$TID" 300
sudo lxc-ls | grep -q pv100 && fail "Container existiert noch"
sudo nft list table bridge pombot | grep -q pv100 && fail "Firewall-Regeln wurden nicht entfernt"
api GET /api/pools | json '[(p["name"], p["used"]) for p in d]'
end

step "Gemeinsamer Speicher (NFS): anlegen, auf dem Node einhängen"
sudo mkdir -p /srv/nfsshared
echo "/srv/nfsshared 127.0.0.1(rw,sync,no_root_squash,no_subtree_check)" | sudo tee -a /etc/exports >/dev/null
sudo exportfs -ra
STO="$(api POST /api/admin/storages '{"name":"ci-nas","type":"nfs","config":{"server":"127.0.0.1","export":"/srv/nfsshared","options":"vers=4,hard,timeo=100,retrans=3"}}')"
echo "$STO"
SID="$(echo "$STO" | json 'd["id"]')"
echo "$STO" | json 'd["nodes"][0]["mounted"]' | grep -q True || fail "NFS-Speicher auf dem Node nicht eingehängt"
mountpoint -q "/mnt/pombot-storage/$SID" || fail "/mnt/pombot-storage/$SID ist kein Mountpoint"
api GET /api/storages | json '[(s["name"], s["nodes"]) for s in d]'
end

step "Container auf gemeinsamem Speicher"
RES="$(api POST /api/guests "{\"template_id\":$TPL,\"name\":\"ci-shared\",\"hostname\":\"ci-shared\",\"cores\":1,\"memory_mb\":512,\"disk_gb\":4,\"storage_id\":$SID,\"password\":\"CiTestPasswort1\"}")"
echo "$RES"
SGID="$(echo "$RES" | json 'd["guest_id"]')"
SVMID="$(echo "$RES" | json 'd["vmid"]')"
SN="pv$SVMID"
wait_task "$(echo "$RES" | json 'd["task_id"]')" 1200
ls -la "/var/lib/lxc/$SN" "/var/lib/pombot/guests/$SN"
[ -L "/var/lib/lxc/$SN" ] || fail "/var/lib/lxc/$SN ist kein Symlink auf den Speicher"
sudo test -f "/srv/nfsshared/guests/$SN/lxc/rootdev" || fail "rootdev liegt nicht auf dem NFS-Speicher"
sudo lxc-info -n "$SN" -s | grep -q RUNNING || fail "Container auf NFS läuft nicht"
api GET "/api/guests/$SGID" | json 'd["storage"], d["ha"]'
end

step "Backup und Wiederherstellung eines Speicher-Containers"
wait_task "$(api POST "/api/guests/$SGID/backups" '{}' | json 'd["task_id"]')" 900
SFILE="$(api GET "/api/guests/$SGID/backups" | json "[b['file'] for b in d['items'] if b['location'] is None][0]")"
sudo tar -tzf "/var/lib/pombot/backups/$SFILE" | grep -q "^$SN/rootdev$" || fail "Backup enthält nur den Symlink statt der Daten"
wait_task "$(api POST "/api/guests/$SGID/backups/$SFILE/restore" | json 'd["task_id"]')" 900
[ -L "/var/lib/lxc/$SN" ] || fail "Nach der Wiederherstellung liegt der Container nicht mehr auf dem Speicher"
sudo lxc-info -n "$SN" -s | grep -q RUNNING || fail "Container läuft nach Wiederherstellung nicht"
end

step "HA: Lease, Autostart, Startsperre bei fremder Lease"
api PATCH "/api/guests/$SGID" '{"ha":true}' | json 'd["ha"]' | grep -q True || fail "HA ließ sich nicht einschalten"
sleep 8
LEASE="/srv/nfsshared/guests/$SN/.pombot-lease.json"
sudo cat "$LEASE"; echo
sudo python3 -c "import json;d=json.load(open('$LEASE'));assert d['node']==open('/etc/machine-id').read().strip()" || fail "Lease gehört nicht diesem Node"
sudo grep -q "lxc.start.auto = 0" "/var/lib/lxc/$SN/config" || fail "Autostart eines HA-Servers wurde nicht abgeschaltet"
api POST "/api/guests/$SGID/action" '{"action":"stop"}' | json 'd["state"]'
sudo python3 -c "import json,time;json.dump({'node':'fremder-node','hostname':'node-b','ts':time.time()},open('$LEASE','w'))"
OUT="$(api POST "/api/guests/$SGID/action" '{"action":"start"}')"
echo "$OUT"
echo "$OUT" | grep -q "Lease" || fail "Start trotz fremder Lease erlaubt"
sudo lxc-info -n "$SN" -s | grep -q STOPPED || fail "Container wurde trotz fremder Lease gestartet"
sudo python3 -c "import json,time;json.dump({'node':'fremder-node','hostname':'node-b','ts':time.time()-300},open('$LEASE','w'))"
api POST "/api/guests/$SGID/action" '{"action":"start"}' | json 'd["state"]' | grep -q running || fail "Start nach abgelaufener Lease nicht möglich"
end

step "Abmelden und Übernehmen (wie bei Umzug/HA, hier auf demselben Node)"
TOKEN="$(sudo grep '^POMBOT_AGENT_TOKEN=' /etc/pombot/agent.env | cut -d= -f2-)"
agent() { curl -sk -X "$1" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" ${3:+-d "$3"} "https://127.0.0.1:8007$2"; }
api POST "/api/guests/$SGID/action" '{"action":"stop"}' | json 'd["state"]'
agent POST "/guests/$SN/release" | grep -q ok || fail "Abmelden fehlgeschlagen"
sudo lxc-ls | grep -qw "$SN" && fail "Container nach dem Abmelden noch registriert"
sudo test -f "/srv/nfsshared/guests/$SN/lxc/rootdev" || fail "Daten nach dem Abmelden nicht mehr auf dem Speicher"
sudo test -e "$LEASE" && fail "Eigene Lease wurde beim Abmelden nicht freigegeben"
agent POST "/guests/$SN/adopt" "{\"type\":\"lxc\",\"storage_id\":\"$SID\",\"ha\":true}" | grep -q ok || fail "Übernahme fehlgeschlagen"
api POST "/api/guests/$SGID/action" '{"action":"start"}' | json 'd["state"]' | grep -q running || fail "Container startet nach der Übernahme nicht"
sudo lxc-attach -n "$SN" -- hostname
end

step "Speicher-Container löschen"
api DELETE "/api/admin/storages/$SID" | grep -q "noch Server" || fail "Speicher mit Servern hätte nicht gelöscht werden dürfen"
wait_task "$(api DELETE "/api/guests/$SGID" | json 'd["task_id"]')" 300
sudo test -e "/srv/nfsshared/guests/$SN" && fail "Daten auf dem Speicher wurden nicht gelöscht"
[ -e "/var/lib/lxc/$SN" ] || [ -L "/var/lib/lxc/$SN" ] && fail "Symlink unter /var/lib/lxc blieb zurück"
api DELETE "/api/admin/storages/$SID" | grep -q ok || fail "Leerer Speicher ließ sich nicht entfernen"
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
  sudo virsh dumpxml "pv$KVMID" | grep -q "<serial" || fail "VM hat keine serielle Konsole (ohne sie bootet das Cloud-Image nicht)"
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

step "ISO-Bibliothek: Upload in Stücken, VM aus ISO, CD-Laufwerk"
mkdir -p /tmp/isosrc && echo "PomBot CI" > /tmp/isosrc/readme.txt && head -c 40M /dev/urandom > /tmp/isosrc/fill.bin
xorriso -as mkisofs -quiet -o /tmp/ci-test.iso -V CITEST /tmp/isosrc
SIZE=$(stat -c %s /tmp/ci-test.iso)
UP="$(api POST "/api/nodes/$(api GET /api/nodes | json 'd[0]["id"]')/isos/uploads" | json 'd["upload_id"]')"
NID="$(api GET /api/nodes | json 'd[0]["id"]')"
OFF=0; CHUNK=$((16 * 1024 * 1024))
while [ "$OFF" -lt "$SIZE" ]; do
  OFF="$(dd if=/tmp/ci-test.iso bs=1M iflag=skip_bytes,count_bytes skip="$OFF" count="$CHUNK" status=none | curl -sk -b "$JAR" -X PUT -H "X-PomBot: 1" -H "Content-Type: application/octet-stream" \
        --data-binary @- "$API/api/nodes/$NID/isos/uploads/$UP?offset=$OFF" | json 'd["offset"]')"
  echo "hochgeladen: $OFF / $SIZE"
done
api POST "/api/nodes/$NID/isos/uploads/$UP/finish" "{\"name\":\"ci-test.iso\",\"size\":$SIZE}" | tee /dev/stderr | grep -q '"file":"ci-test.iso"' || fail "ISO-Upload fehlgeschlagen"
test -f /var/lib/pombot/iso/ci-test.iso || fail "ISO liegt nicht auf dem Node"
RES="$(api POST /api/guests "{\"iso_file\":\"ci-test.iso\",\"node\":$NID,\"name\":\"ci-iso\",\"hostname\":\"ci-iso\",\"cores\":1,\"memory_mb\":512,\"disk_gb\":5,\"ipv4_pool\":\"none\"}")"
echo "$RES"
IGID="$(echo "$RES" | json 'd["guest_id"]')"; IVMID="$(echo "$RES" | json 'd["vmid"]')"
wait_task "$(echo "$RES" | json 'd["task_id"]')" 300
sudo virsh dumpxml "pv$IVMID" | grep -q "ci-test.iso" || fail "ISO nicht als Laufwerk eingebunden"
api POST "/api/guests/$IGID/cdrom" '{"iso_file":"ci-test.iso","boot_cdrom":true}' | tee /dev/stderr | grep -q '"boot_cdrom":true' || fail "CD-Laufwerk nicht gesetzt"
sudo virsh dumpxml --inactive "pv$IVMID" | grep -A1 "<type" | tee /dev/stderr
sudo virsh dumpxml --inactive "pv$IVMID" | grep -m1 "<boot dev=" | grep -q cdrom || fail "Start von CD nicht als erstes eingestellt"
api POST "/api/guests/$IGID/cdrom" '{"iso_file":null,"boot_cdrom":false}' | grep -q '"boot_cdrom":false' || fail "Auswerfen fehlgeschlagen"
wait_task "$(api DELETE "/api/guests/$IGID" | json 'd["task_id"]')" 300
api DELETE "/api/nodes/$NID/isos/ci-test.iso" | grep -q ok || fail "ISO löschen fehlgeschlagen"
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
RPOOL6_ID="$(api POST /api/pools "{\"name\":\"zusatz6\",\"mode\":\"routed\",\"node_id\":$NODE_ID,\"network\":\"2001:db8:99::/64\"}" | json 'd["id"]')"
end

step "Container im gerouteten Modus (IPv4 + IPv6)"
RES="$(api POST /api/guests "{\"template_id\":$TPL,\"name\":\"ci-routed\",\"hostname\":\"ci-routed\",\"cores\":1,\"memory_mb\":512,\"disk_gb\":4,\"ipv4_pool\":$RPOOL_ID,\"ipv6_pool\":$RPOOL6_ID,\"password\":\"CiTestPasswort1\"}")"
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
IP6="$(api GET "/api/guests/$RGID" | json '[i["address"] for i in d["ips"] if i["version"]==6][0]')"
echo "IPv6 des Containers: $IP6"
sudo lxc-attach -n "pv$RVMID" -- ip -6 addr show eth0 | grep -q "$IP6/128" || fail "IPv6 /128 nicht im Container"
sudo lxc-attach -n "pv$RVMID" -- ip -6 route | tee /dev/stderr | grep -q "default via fe80::1" || fail "IPv6-Default-Route über fe80::1 fehlt"
ip -6 route show "$IP6" | tee /dev/stderr | grep -q pbr0 || fail "IPv6-Route über pbr0 fehlt"
ip -6 neigh show proxy | grep -q "$IP6" || fail "Proxy-NDP-Eintrag fehlt"
ip -6 addr show dev pbr0 | grep -q "fe80::1" || fail "fe80::1 fehlt auf pbr0"
sudo lxc-attach -n "pv$RVMID" -- ping -6 -c 2 -W 2 fe80::1%eth0 || fail "Container erreicht sein IPv6-Gateway nicht"
end

step "Geroutet: Löschen entfernt Route"
wait_task "$(api DELETE "/api/guests/$RGID" | json 'd["task_id"]')" 300
ip route show 192.0.2.10 | grep -q pbr0 && fail "Route wurde nicht entfernt"
api GET /api/pools | json '[(p["name"], p["used"], p["size"]) for p in d]'
end

step "Updates: Prüfung über die API"
systemctl is-enabled pombot-update.path | grep -q enabled || fail "pombot-update.path ist nicht aktiviert"
UPD="$(api POST /api/admin/update/check)"
echo "$UPD" | json '{k: d[k] for k in ("current", "latest", "available", "helper", "check_hours")}'
echo "$UPD" | json 'd["helper"]' | grep -q True || fail "Update-Dienst wird vom Panel nicht erkannt"
TARGET="$(echo "$UPD" | json 'd["latest"]["tag"]')"
[ -n "$TARGET" ] || fail "Neueste Version konnte nicht ermittelt werden"
echo "Testziel (letztes veröffentlichtes Release): $TARGET"
end

step "Updates: Agent aktualisiert sich selbst (Testziel $TARGET)"
TOKEN="$(sudo grep '^POMBOT_AGENT_TOKEN=' /etc/pombot/agent.env | cut -d= -f2-)"
agent POST /system/update "{\"tag\":\"$TARGET\"}" | tee /dev/stderr | grep -q '"ok":true' || fail "Agent-Update ließ sich nicht starten"
for i in $(seq 1 60); do
  STATE="$(sudo cat /var/log/pombot-agent-update.json 2>/dev/null | json 'd["state"]' 2>/dev/null || true)"
  [ "$STATE" = "ok" ] || [ "$STATE" = "error" ] && break
  sleep 5
done
sudo cat /var/log/pombot-agent-update.log | tail -15
[ "$STATE" = "ok" ] || fail "Agent-Update endete mit '$STATE'"
dpkg -s pombot-agent | grep "^Version: ${TARGET#v}$" || fail "pombot-agent hat nicht die Version ${TARGET#v}"
end

step "Updates: Panel über den Update-Dienst (Auftrag wie aus dem Panel)"
echo "$TARGET" | sudo -u pombot tee /var/lib/pombot-panel/update/request >/dev/null
for i in $(seq 1 90); do
  STATE="$(sudo cat /var/log/pombot-update.json 2>/dev/null | json 'd["state"]' 2>/dev/null || true)"
  [ "$STATE" = "ok" ] || [ "$STATE" = "error" ] && break
  sleep 5
done
sudo tail -20 /var/log/pombot-update.log
[ "$STATE" = "ok" ] || fail "Panel-Update endete mit '$STATE'"
[ -e /var/lib/pombot-panel/update/request ] && fail "Auftrag wurde nicht entfernt"
dpkg -s pombot-panel | grep "^Version: ${TARGET#v}$" || fail "pombot-panel hat nicht die Version ${TARGET#v}"
for i in $(seq 1 30); do curl -sk -m 5 "$API/api/auth/config" | grep -q "\"${TARGET#v}\"" && break; sleep 3; done
curl -sk "$API/api/auth/config"; echo
curl -sk "$API/api/auth/config" | grep -q "\"${TARGET#v}\"" || fail "Panel läuft nach dem Update nicht mit ${TARGET#v}"
end

echo "Integrationstest erfolgreich."
