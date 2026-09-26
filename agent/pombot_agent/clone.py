"""Server klonen (vollständige Kopie auf demselben Node, wie „Full Clone“ bei Proxmox).

Der Klon bekommt einen neuen Namen, eigene MAC und IPs, einen eigenen Hostnamen und ein neues
root-Passwort. VMs mit cloud-init richten sich über einen neuen Seed (neue Instanz-ID) selbst neu ein –
dabei entstehen auch neue SSH-Hostschlüssel. Container werden im gestoppten Zustand kopiert.
"""
import os
import shlex
import time
from pathlib import Path

from . import cloudinit, kvm, lxc, storage
from .config import LXC_PATH
from .util import CmdError, check_name, run

CLONE_SNAP = "pvclone"


def clone(job, src: str, spec: dict) -> dict:
    check_name(src)
    check_name(spec["name"])
    if lxc.exists(spec["name"]) or kvm.exists(spec["name"]):
        raise CmdError(f"{spec['name']} existiert bereits")
    if lxc.exists(src):
        return _clone_lxc(job, src, spec)
    if kvm.exists(src):
        return _clone_kvm(job, src, spec)
    raise CmdError("Quell-Server nicht gefunden")


def _clone_kvm(job, src: str, spec: dict) -> dict:
    name = spec["name"]
    src_disk = Path(kvm.config(src)["disk"])
    src_dir = (kvm._guest_dir(src)).resolve()
    d = storage.prepare_guest_dir(spec.get("storage_id"), name)
    os.chmod(d, 0o755)
    disk = d / "disk.qcow2"
    try:
        if kvm.state(src) in ("running", "paused"):
            job.write("Quelle läuft – kopiere aus einem kurzzeitigen Snapshot (Quelle läuft weiter) …")
            run(["virsh", "snapshot-delete", src, CLONE_SNAP], check=False)
            run(["virsh", "snapshot-create-as", "--domain", src, "--name", CLONE_SNAP, "--atomic"], job=job, timeout=1800)
            try:
                run(["qemu-img", "convert", "-U", "-O", "qcow2", "-l", f"snapshot.name={CLONE_SNAP}", src_disk, disk],
                    job=job, timeout=6 * 3600)
            finally:
                run(["virsh", "snapshot-delete", src, CLONE_SNAP], check=False)
        else:
            job.write("Kopiere Festplatte …")
            run(["qemu-img", "convert", "-O", "qcow2", src_disk, disk], job=job, timeout=6 * 3600)
        run(["qemu-img", "resize", disk, f"{int(spec['disk_gb'])}G"], job=job, check=False)
        cdrom = None
        seeded = (src_dir / "seed.iso").exists()
        if seeded:
            cdrom = cloudinit.build_seed(d, {**spec, "instance_suffix": f"clone{int(time.time())}"}, job)
        xml_path = d / "domain.xml"
        xml_path.write_text(kvm.domain_xml(spec, disk, cdrom, os.path.exists("/dev/kvm")))
        run(["virsh", "define", xml_path], job=job)
        run(["virsh", "autostart", name] + ([] if spec.get("onboot", True) else ["--disable"]), job=job)
        if seeded:
            run(["virsh", "start", name], job=job)
            job.write("Klon gestartet – cloud-init setzt Hostname, Netzwerk und Passwort beim ersten Start.")
        else:
            job.write("HINWEIS: Die Quelle wurde per ISO installiert (ohne cloud-init). Der Klon bleibt gestoppt, weil er "
                      "sonst mit der IP der Quelle starten würde – bitte IP im System anpassen: "
                      + ", ".join(f"{ip['address']}/{ip['prefix']}" for ip in spec.get("ips") or []))
    except Exception:
        job.write("Räume nach Fehler auf …")
        kvm._remove(name)
        storage.remove_guest_dirs(name)
        raise
    return {"state": kvm.state(name)}


def _clone_lxc(job, src: str, spec: dict) -> dict:
    name = spec["name"]
    was_running = lxc.state(src) in ("running", "paused")
    src_dir = (LXC_PATH / src).resolve()
    if spec.get("storage_id"):
        target = storage.prepare_guest_dir(spec["storage_id"], name) / "lxc"
    else:
        target = LXC_PATH / name
    if target.exists():
        raise CmdError(f"{target} existiert bereits")
    try:
        if was_running:
            job.write("Container wird für eine konsistente Kopie kurz gestoppt …")
            lxc._stop(src, job)
        try:
            job.write("Kopiere Container …")
            run(["cp", "-a", "--sparse=always", src_dir, target], job=job, timeout=6 * 3600)
        finally:
            if was_running:
                lxc._start(src, job)
                job.write("Quelle läuft wieder.")
        if spec.get("storage_id"):
            (LXC_PATH / name).symlink_to(target)

        # Pfade und Identität in der Konfiguration auf den Klon umstellen
        cfg = LXC_PATH / name / "config"
        text = cfg.read_text().replace(f"{LXC_PATH}/{src}/", f"{LXC_PATH}/{name}/")
        text = text.replace(f"{src_dir}/", f"{LXC_PATH}/{name}/")
        cfg.write_text(text)
        for key, value in lxc._limits(spec["cores"], spec["memory_mb"]).items():
            lxc.set_config(name, key, value)
        lxc.set_config(name, "lxc.uts.name", (spec.get("hostname") or name).split(".")[0])
        lxc.set_config(name, "lxc.net.0.hwaddr", spec["mac"])
        lxc.set_config(name, "lxc.net.0.link", spec["bridge"])
        lxc.set_config(name, "lxc.start.auto", "1" if spec.get("onboot", True) else "0")
        lxc._set_net_config(name, spec.get("ips") or [])

        job.write("Starte Klon und richte ihn ein …")
        lxc._start(name, job)
        time.sleep(3)
        lxc._log_output(job, lxc.attach(name, lxc._network_script(spec), job=job, log=False))
        # eigene Identität: neue machine-id und neue SSH-Hostschlüssel
        lxc.attach(name, "rm -f /etc/machine-id /var/lib/dbus/machine-id; "
                         "(systemd-machine-id-setup || dbus-uuidgen --ensure=/etc/machine-id) >/dev/null 2>&1 || true; "
                         "rm -f /etc/ssh/ssh_host_*; (ssh-keygen -A >/dev/null 2>&1 || true)", log=False)
        if spec.get("password"):
            run(["lxc-attach", "-n", name, *lxc.ATTACH, "--", "chpasswd"],
                input_text=f"root:{spec['password']}\n", log=False)
        keys = [k.strip() for k in (spec.get("ssh_keys") or []) if k.strip()]
        if keys:
            lxc.attach(name, "mkdir -p /root/.ssh && chmod 700 /root/.ssh && printf '%s\\n' "
                       + " ".join(shlex.quote(k) for k in keys) + " >> /root/.ssh/authorized_keys", log=False)
        lxc.attach(name, "systemctl restart ssh 2>/dev/null || rc-service sshd restart 2>/dev/null || true", log=False)
    except Exception:
        job.write("Räume nach Fehler auf …")
        run(["lxc-stop", "-n", name, "-k"], check=False)
        if (LXC_PATH / name).is_symlink():
            (LXC_PATH / name).unlink()
            run(["rm", "-rf", target], check=False)
        elif target.exists() and target != src_dir:
            run(["rm", "-rf", target], check=False)
        storage.remove_guest_dirs(name)
        raise
    return {"state": lxc.state(name)}
