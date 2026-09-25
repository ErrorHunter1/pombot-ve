# PomBot VE – eigene Proxmox-Alternative

Web-Panel zum Verwalten von **KVM-VMs** und **LXC-Containern** auf beliebig vielen Servern (Nodes).
Neue Server werden über eine interaktive Oberfläche bestellt. **IP-Adresse, Gateway, DNS, Hostname,
root-Passwort und SSH-Schlüssel werden automatisch eingerichtet.** Login per **Discord** oder Passwort,
mit Benutzerrollen und Kontingenten.

Läuft auf **Debian 12/13** und **Ubuntu 22.04/24.04**.

## Aufbau

```
 Browser ──HTTPS──▶ Panel (FastAPI + SQLite, Web-UI)  ──HTTPS + Token + Zertifikat-Pinning──▶ Agent auf jedem Node
                    Benutzer, Discord-Login, IP-Pools,                                          libvirt/QEMU (VMs)
                    Kontingente, Aufgaben, Audit-Log                                            LXC (Container)
```

| Ordner | Inhalt |
|---|---|
| `panel/` | Web-Oberfläche und API (läuft als Benutzer `pombot`) |
| `agent/` | Dienst auf jedem Virtualisierungs-Host (läuft als root, Port 8007) |
| `install.sh` | Installer für das Panel (optional gleich mit Agent) |
| `agent/install-agent.sh` | Installer für einen Node (wird vom Panel automatisch per SSH ausgeführt) |

## Installation

### Variante A: Pakete (empfohlen)

Die `.deb`-Dateien aus dem neuesten [Release](https://github.com/ErrorHunter1/pombot-ve/releases) laden
und auf den Server kopieren (das Repository ist privat, z. B. mit
`gh release download --repo ErrorHunter1/pombot-ve --pattern '*.deb'`):

```bash
# Panel (Web-Oberfläche)
sudo apt install ./pombot-panel_0.2.0_all.deb
sudo cat /root/pombot-admin.txt          # Adresse + Admin-Passwort

# Node (auch auf demselben Server möglich) – gibt einen Join-Code aus
sudo apt install ./pombot-agent_0.2.0_all.deb
```

`apt` installiert automatisch alle Abhängigkeiten (QEMU, libvirt, LXC, nftables …). Updates: neues Paket
genauso installieren, Konfiguration und Daten bleiben erhalten. Selbst bauen: `bash packaging/build-deb.sh`.

### Variante B: aus dem Quellcode

Projekt auf den Server kopieren, dann als root:

```bash
# Panel + diesen Server gleich als ersten Node einrichten
sudo bash install.sh --with-agent

# nur das Panel (Nodes werden später über die GUI hinzugefügt)
sudo bash install.sh --url https://panel.example.com:8443
```

Am Ende werden die Panel-Adresse und das Admin-Passwort ausgegeben.

### Weitere Nodes hinzufügen

In der Oberfläche **Nodes → Node hinzufügen**:

- **Automatisch per SSH:** Adresse und root-Passwort (oder SSH-Schlüssel) eingeben. Das Panel installiert
  KVM, LXC und den Agent und bindet den Node ein. Optional wird die Netzwerk-Bridge `vmbr0` angelegt.
  Zugangsdaten werden nicht gespeichert.
- **Join-Code:** `agent/` auf den Server kopieren, `sudo bash install-agent.sh --allow <Panel-IP>` ausführen
  und den ausgegebenen `POMBOT_JOIN=…` im Panel einfügen.

Der Agent akzeptiert nur Anfragen mit seinem Token und (per `--allow`) nur von der Panel-IP. Das Panel
prüft das Zertifikat jedes Agents fest (Pinning).

### Netzwerk / IP-Pools

1. Jeder Node braucht eine Linux-Bridge (Standard `vmbr0`), an der die VMs hängen. Beim Hinzufügen per SSH
   kann sie automatisch angelegt werden (netplan oder ifupdown, Backup der alten Konfiguration unter
   `/etc/pombot/network-backup-*`).
2. Unter **IP-Pools** die Adressen deines Hosters eintragen (Netz, Gateway, DNS, optional Bereich).
   Das Gateway darf außerhalb des Netzes liegen (z. B. Hetzner `172.31.1.1` bei /32-Adressen) und wird
   dann on-link geroutet.
3. Beim Erstellen eines Servers wird die nächste freie Adresse vergeben und per cloud-init (VM) bzw.
   direkt im Container (netplan / systemd-networkd / ifupdown) eingetragen. Beim Löschen wird sie frei.

> Viele Hoster (z. B. Hetzner Dedicated) lassen zusätzliche IPs nur mit **eigener MAC-Adresse** oder
> **gerouteten Subnetzen** zu. Für gebridgte Einzel-IPs muss dort ggf. eine virtuelle MAC beantragt werden.

### Discord-Login

1. <https://discord.com/developers/applications> → neue Anwendung → **OAuth2**
2. Redirect eintragen: `https://<panel-adresse>/api/auth/discord/callback` (muss exakt `POMBOT_BASE_URL` entsprechen)
3. In `/etc/pombot/panel.env` eintragen:
   ```
   DISCORD_CLIENT_ID=…
   DISCORD_CLIENT_SECRET=…
   DISCORD_GUILD_ID=…          # optional: nur Mitglieder dieses Discord-Servers
   DISCORD_ADMIN_IDS=…         # optional: diese Discord-IDs werden Admin
   POMBOT_REGISTRATION=approval # open | approval | closed
   ```
4. `systemctl restart pombot-panel`

Neue Discord-Benutzer bekommen das Standard-Kontingent aus `panel.env`. Mit `approval` müssen sie
zuerst von einem Admin unter **Benutzer** freigeschaltet werden. Der allererste Benutzer wird automatisch Admin.
Bestehende Konten können Discord unter **Mein Konto** verknüpfen.

## Funktionen

- **Server:** VMs (Cloud-Images oder ISO) und Container (Debian 12/13, Ubuntu 22.04/24.04 vorkonfiguriert,
  weitere Vorlagen per GUI), automatische Platzierung auf dem Node mit dem meisten freien RAM
- **Steuerung:** Starten, Herunterfahren, Neustart, Stopp, Pausieren; Live-Auslastung mit Verlauf
- **Konsole im Browser:** noVNC für VMs, Terminal (xterm.js) für Container, Root-Shell für Nodes (Admin)
- **Ressourcen ändern:** CPU, RAM, Festplatte vergrößern (mit Kontingentprüfung)
- **Snapshots** (erstellen, zurückspielen, löschen) und **Backups** (erstellen, wiederherstellen, löschen)
- **Zeitgesteuerte Backups** pro Server (täglich/wöchentlich, Uhrzeit, Anzahl aufzubewahrender Backups);
  verpasste Termine werden nachgeholt, pro Node läuft immer nur ein automatisches Backup gleichzeitig
- **Firewall pro Server** (nftables auf dem Node): Regeln für ein-/ausgehend, TCP/UDP/ICMP, Ports und
  Portbereiche, Quell-/Zielnetze, Standardaktion, Vorlagen für SSH/Web/Ping/DNS/Mail
- **Spoofing-Schutz**: Server können nur mit ihren eigenen IP-Adressen senden (standardmäßig immer aktiv)
- **Neu installieren** mit gleicher IP, **root-Passwort zurücksetzen**
- **Benutzer:** Admin/Benutzer, Kontingente (Server, CPU, RAM, Speicher, IPs), Freischaltung, lokale Konten
- **Aufgaben** mit Live-Log, **Audit-Protokoll** aller Aktionen
- Hell-/Dunkelmodus, auch auf dem Handy nutzbar

Noch **nicht** enthalten (Ideen für später): Live-Migration zwischen Nodes, Cluster-Storage (Ceph/NFS),
Backups auf externen Speicher, HA, IPv6-Router-Advertisements.

## Entwicklung & Releases

- Jeder Push auf `main` baut die Pakete und führt einen **Integrationstest auf einem echten Ubuntu-Host**
  aus (`packaging/ci-integration.sh`): Pakete installieren, Node einbinden, LXC-Container mit automatischer
  IP anlegen, Firewall, Backup, Snapshot und Löschen prüfen.
- Ein neues Release entsteht automatisch mit einem Tag: Version in `VERSION`, `panel/pombot_panel/config.py`
  und `agent/pombot_agent/config.py` erhöhen, dann `git tag v0.3.0 && git push --tags`.

## Verwaltung

```bash
pombot-panel list-users
pombot-panel reset-password admin
pombot-panel create-admin --username chef
systemctl status pombot-panel        # auf dem Panel
systemctl status pombot-agent        # auf jedem Node
journalctl -u pombot-agent -f
```

Dateien:

| Pfad | Zweck |
|---|---|
| `/etc/pombot/panel.env` | Panel-Konfiguration (Discord, Kontingente, URL) |
| `/var/lib/pombot-panel/panel.db` | Datenbank des Panels |
| `/etc/pombot/agent.env` | Agent-Token, Port, erlaubte IPs |
| `/var/lib/pombot/{images,iso,guests,backups}` | Images, VM-Festplatten und Backups auf dem Node |
| `/var/lib/pombot/firewall/` | Firewall-Regeln pro Server (nftables-Tabelle `bridge pombot`) |
| `/var/lib/lxc/pvXXX` | Container |

Das Panel nutzt ein selbstsigniertes Zertifikat (`/etc/pombot/panel-cert.pem`). Für ein echtes Zertifikat
diese Dateien ersetzen oder einen Reverse-Proxy (nginx/Caddy) davorschalten.

**Firewall:** Panel-Port (Standard 8443/tcp) öffnen. Auf Nodes Port 8007/tcp nur für die Panel-IP freigeben.

## Entwicklung (ohne echten Host)

```bash
cd panel
python -m venv venv && venv/bin/pip install -r requirements.txt
POMBOT_PANEL_ENV=none POMBOT_DB_URL=sqlite:///dev.db venv/bin/python -m pombot_panel.cli create-admin --password admin12345
POMBOT_PANEL_ENV=none POMBOT_DB_URL=sqlite:///dev.db venv/bin/uvicorn pombot_panel.main:app --port 8443
```
