"""Selbst-Update des Agents (vom Panel angestoßen).

Lädt pombot-agent_<version>_all.deb aus den GitHub-Releases und installiert es. Das läuft als eigene
systemd-Unit (systemd-run), weil die Installation den Agent neu startet – ein Kindprozess des Agents würde
dabei mit beendet.
"""
import json
import os
import re
import time
from pathlib import Path

from .util import CmdError, run

REPO = os.environ.get("POMBOT_UPDATE_REPO", "ErrorHunter1/pombot-ve")
STATUS = Path("/var/log/pombot-agent-update.json")
LOG = Path("/var/log/pombot-agent-update.log")
SCRIPT_PATH = Path("/var/lib/pombot/agent-update.sh")
TAG_RE = re.compile(r"^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

SCRIPT = r'''
set -uo pipefail
TAG="$1"; REPO="$2"; VER="${TAG#v}"
STATUS=/var/log/pombot-agent-update.json
status() { printf '{"state":"%s","message":"%s","tag":"%s","time":%s}\n' "$1" "$2" "$TAG" "$(date +%s)" > "$STATUS"; }
exec >>/var/log/pombot-agent-update.log 2>&1
echo "$(date '+%F %T') Agent-Update auf $TAG (github.com/$REPO)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
DEB="$TMP/pombot-agent_${VER}_all.deb"
if ! curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors -o "$DEB" "https://github.com/$REPO/releases/download/$TAG/pombot-agent_${VER}_all.deb"; then
  echo "Download fehlgeschlagen"; status error "Download fehlgeschlagen"; exit 1
fi
status running "Installiere $TAG (Agent startet dabei neu) …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q || echo "WARNUNG: apt-get update fehlgeschlagen"
if apt-get install -y -q --allow-downgrades -o Dpkg::Options::=--force-confold "$DEB"; then
  echo "$(date '+%F %T') Fertig."; status ok "Agent auf $TAG aktualisiert"
else
  echo "$(date '+%F %T') Installation fehlgeschlagen."; status error "Installation fehlgeschlagen – Details im Log"; exit 1
fi
'''


def status() -> dict:
    try:
        data = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        data = {"state": "idle"}
    try:
        data["log"] = LOG.read_text(errors="replace").splitlines()[-40:]
    except OSError:
        data["log"] = []
    return data


def start(tag: str) -> dict:
    if not TAG_RE.match(tag):
        raise CmdError("Ungültige Version")
    if not REPO_RE.match(REPO):
        raise CmdError("Ungültiges Update-Repository")
    current = status()
    if current.get("state") == "running" and time.time() - float(current.get("time", 0)) < 1800:
        raise CmdError("Es läuft bereits ein Update")
    LOG.write_text("")
    STATUS.write_text(json.dumps({"state": "running", "message": "Update gestartet …", "tag": tag, "time": time.time()}))
    # Skript als Datei übergeben: systemd ersetzt ${…} in Befehlszeilen-Argumenten durch eigene (leere)
    # Umgebungsvariablen – als Inline-Skript würde aus ${VER} ein leerer Text.
    SCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCRIPT_PATH.write_text(SCRIPT)
    SCRIPT_PATH.chmod(0o700)
    unit = f"pombot-agent-update-{int(time.time())}"
    run(["systemd-run", f"--unit={unit}", "--collect", "--quiet", "/bin/bash", SCRIPT_PATH, tag, REPO])
    return {"ok": True, "unit": unit}
