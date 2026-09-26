"""Anwendungen (z. B. mailcow, Pterodactyl) beim Erstellen eines Servers automatisch installieren.

Das Panel schickt ein fertiges Installationsskript mit. Im Server landen:
  /root/pombot-app.sh       das Skript
  /root/pombot-app-run.sh   startet es und hält den Zustand fest
  /var/log/pombot-app.log   Ausgabe der Installation
  /var/log/pombot-app.state running | ok | error <code>
  /root/pombot-app-info.txt Zugangsdaten und nächste Schritte (vom Skript geschrieben)
Die Installation läuft als eigene systemd-Unit im Server, damit sie unabhängig vom Anlegen weiterläuft.
"""
import base64
import json

from .util import CmdError, run

APP_SCRIPT = "/root/pombot-app.sh"
RUNNER_PATH = "/root/pombot-app-run.sh"
RUNNER = """#!/bin/bash
echo running > /var/log/pombot-app.state
bash /root/pombot-app.sh > /var/log/pombot-app.log 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then echo ok > /var/log/pombot-app.state; else echo "error $rc" > /var/log/pombot-app.state; fi
"""
START = ["systemd-run", "--unit=pombot-app", "--collect", "--property=TimeoutStartSec=infinity", "/bin/bash", RUNNER_PATH]


def cloud_init_parts(script: str) -> tuple[list[dict], list[list[str]]]:
    """write_files- und runcmd-Einträge für cloud-init (KVM)."""
    files = [{"path": APP_SCRIPT, "content": script, "permissions": "0700"},
             {"path": RUNNER_PATH, "content": RUNNER, "permissions": "0700"}]
    cmds = [["sh", "-c", "echo queued > /var/log/pombot-app.state"], START]
    return files, cmds


def start_in_container(name: str, script: str, job=None) -> None:
    """Skript in einen laufenden Container legen und im Hintergrund starten (LXC)."""
    from .lxc import ATTACH
    for path, content in ((APP_SCRIPT, script), (RUNNER_PATH, RUNNER)):
        run(["lxc-attach", "-n", name, *ATTACH, "--", "/bin/sh", "-c", f"umask 077; cat > {path}; chmod 700 {path}"],
            input_text=content, log=False)
    run(["lxc-attach", "-n", name, *ATTACH, "--", "/bin/sh", "-c",
         "echo queued > /var/log/pombot-app.state; " + " ".join(START) + " >/dev/null 2>&1 || "
         f"(nohup setsid /bin/bash {RUNNER_PATH} >/dev/null 2>&1 &)"], log=False)
    if job:
        job.write("Installation der Anwendung läuft jetzt im Hintergrund im Container weiter.")


# ---------------------------------------------------------------- Zustand abfragen

def _lxc_read(name: str, path: str) -> str | None:
    from .lxc import ATTACH
    try:
        return run(["lxc-attach", "-n", name, *ATTACH, "--", "/bin/sh", "-c", f"cat {path} 2>/dev/null || true"],
                   timeout=20, log=False)
    except CmdError:
        return None


def _qga(name: str, cmd: dict) -> dict:
    out = run(["virsh", "qemu-agent-command", name, json.dumps(cmd), "--timeout", "10"], timeout=20, log=False)
    return json.loads(out).get("return", {})


def _kvm_read(name: str, path: str) -> str | None:
    """Datei über den qemu-guest-agent lesen (None = Agent noch nicht bereit oder Datei fehlt)."""
    try:
        handle = _qga(name, {"execute": "guest-file-open", "arguments": {"path": path, "mode": "r"}})
    except (CmdError, ValueError):
        return None
    data = b""
    try:
        for _ in range(8):  # höchstens 8 MB
            chunk = _qga(name, {"execute": "guest-file-read", "arguments": {"handle": handle, "count": 1 << 20}})
            data += base64.b64decode(chunk.get("buf-b64", ""))
            if chunk.get("eof") or not chunk.get("count"):
                break
    except (CmdError, ValueError):
        pass
    finally:
        try:
            _qga(name, {"execute": "guest-file-close", "arguments": {"handle": handle}})
        except (CmdError, ValueError):
            pass
    return data.decode(errors="replace")


def status(name: str, gtype: str) -> dict:
    read = _kvm_read if gtype == "kvm" else _lxc_read
    raw_state = read(name, "/var/log/pombot-app.state")
    if raw_state is None:
        return {"state": "unknown", "log": [], "info": ""}
    raw_state = raw_state.strip()
    state = raw_state.split()[0] if raw_state else "pending"
    log = (read(name, "/var/log/pombot-app.log") or "").splitlines()[-80:]
    info = read(name, "/root/pombot-app-info.txt") or ""
    return {"state": state, "detail": raw_state, "log": log, "info": info[:20000]}
