#!/usr/bin/env bash
# Findet heraus, welche VM-Einstellungen bei verschachtelter Virtualisierung (Node ist selbst eine VM,
# wie bei vielen VPS-Hostern) zuverlässig booten. Startet für jede Kombination aus CPU-Modell und
# serieller Konsole eine echte Debian-12-VM über den Agent und prüft, ob SSH erreichbar wird.
set -uo pipefail

AGENT=https://127.0.0.1:8007
RESULTS=/tmp/diag/kvm-matrix.txt
mkdir -p /tmp/diag
: > "$RESULTS"

sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q ./dist/pombot-agent_*.deb > /dev/null
sudo chmod 666 /dev/kvm || true
systemd-detect-virt || true
grep -m1 "model name" /proc/cpuinfo

sudo ip link add vmbr0 type bridge && sudo ip addr add 10.99.0.1/24 dev vmbr0 && sudo ip link set vmbr0 up
sudo sysctl -qw net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -s 10.99.0.0/24 ! -d 10.99.0.0/24 -j MASQUERADE

TOKEN="$(sudo sed -n 's/^POMBOT_AGENT_TOKEN=//p' /etc/pombot/agent.env)"
agent() { curl -sk -X "$1" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" ${3:+-d "$3"} "$AGENT$2"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
wait_job() {
  local id="$1" st
  for _ in $(seq 1 240); do
    st="$(agent GET "/jobs/$id" | json 'd["status"]')"
    [ "$st" != "running" ] && break
    sleep 5
  done
  agent GET "/jobs/$id" | json '"\n".join(d["lines"][-6:])'
  [ "$st" = "ok" ]
}

N=0
for variant in "host-passthrough:1" "host-passthrough:0" "host-model:1" "host-model:0" "x86-64-v2-AES:0" "x86-64-v2-AES:1"; do
  CPU="${variant%%:*}"; SERIAL="${variant##*:}"; N=$((N + 1))
  NAME="pv9$N"; IP="10.99.0.$((20 + N))"
  echo "::group::Variante CPU=$CPU seriell=$SERIAL ($NAME, $IP)"
  sudo sed -i '/^POMBOT_KVM_CPU=/d; /^POMBOT_KVM_SERIAL=/d' /etc/pombot/agent.env
  printf 'POMBOT_KVM_CPU=%s\nPOMBOT_KVM_SERIAL=%s\n' "$CPU" "$SERIAL" | sudo tee -a /etc/pombot/agent.env > /dev/null
  sudo systemctl restart pombot-agent
  for _ in $(seq 1 30); do curl -sk "$AGENT/health" > /dev/null && break; sleep 1; done
  SPEC="{\"name\":\"$NAME\",\"type\":\"kvm\",\"hostname\":\"matrix$N\",\"cores\":2,\"memory_mb\":1024,\"disk_gb\":8,\"bridge\":\"vmbr0\",\"mac\":\"52:54:00:99:00:0$N\",\"ips\":[{\"address\":\"$IP\",\"prefix\":24,\"gateway\":\"10.99.0.1\",\"version\":4}],\"dns\":[\"1.1.1.1\"],\"password\":\"MatrixTest12345\",\"image_url\":\"https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2\"}"
  JOB="$(agent POST /guests "$SPEC" | json 'd["job"]')"
  if ! wait_job "$JOB"; then
    echo "CPU=$CPU seriell=$SERIAL: ERSTELLEN FEHLGESCHLAGEN" | tee -a "$RESULTS"
    echo "::endgroup::"; continue
  fi
  sudo virsh dumpxml "$NAME" | grep -E "<cpu|<model|<serial" || true
  START=$(date +%s); OK=0
  for _ in $(seq 1 48); do
    if timeout 3 bash -c "echo > /dev/tcp/$IP/22" 2>/dev/null; then OK=1; break; fi
    sleep 5
  done
  SECS=$(( $(date +%s) - START ))
  sudo virsh screenshot "$NAME" "/tmp/diag/$NAME-cpu-$CPU-serial-$SERIAL.png" > /dev/null 2>&1
  if [ "$OK" = 1 ]; then
    echo "CPU=$CPU seriell=$SERIAL: OK – SSH nach ${SECS}s" | tee -a "$RESULTS"
  else
    echo "CPU=$CPU seriell=$SERIAL: KEIN BOOT (${SECS}s gewartet)" | tee -a "$RESULTS"
  fi
  wait_job "$(agent DELETE "/guests/$NAME" | json 'd["job"]')" > /dev/null
  echo "::endgroup::"
done

sudo chown -R "$(id -u):$(id -g)" /tmp/diag; chmod -R a+rX /tmp/diag
echo "================ Ergebnis ================"
cat "$RESULTS"
