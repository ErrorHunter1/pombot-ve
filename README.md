# PomBot VE – eigene Proxmox-Alternative Created with KI

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
sudo apt install ./pombot-panel_0.4.3_all.deb
sudo cat /root/pombot-admin.txt          # Adresse + Admin-Passwort

# Node (auch auf demselben Server möglich) – gibt einen Join-Code aus
sudo apt install ./pombot-agent_0.4.3_all.deb
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

Unter **IP-Pools** trägst du die Adressen deines Hosters ein – entweder als **einzelne Adressen**
(z. B. 7 gebuchte Zusatz-IPs, auch als Bereich `91.200.5.20-23`) oder als **ganzes Netz** (CIDR).
Mit **„IPs und Gateway vom Node erkennen“** liest PomBot alle auf dem Server eingetragenen IPv4-Adressen
und das Gateway aus und übernimmt sie per Klick.

Zwei Netzwerk-Modi:

| Modus | Wann | Wie es funktioniert |
|---|---|---|
| **Geroutet** (empfohlen) | Zusatz-IPs ohne eigene MAC-Adresse (skrime, Hetzner, OVH, Netcup …) | Der Node nimmt die IPs per Proxy-ARP entgegen und leitet sie über die interne Bridge `pbr0` weiter. Jeder Server bekommt seine IP als `/32`, Gateway ist automatisch die Haupt-IP des Nodes. Beim Hoster ist nichts einzurichten. |
| **Bridge** | Eigenes Netz/VLAN oder Hoster vergibt pro IP eine MAC | Server hängen direkt an einer Bridge (z. B. `vmbr0`) im Netz des Hosters. |

Im gerouteten Modus nimmt PomBot eine Zusatz-IP automatisch von der Netzwerkkarte des Hosts, falls sie dort
eingetragen war (sonst würde der Host sie selbst beantworten). Die Haupt-IP bleibt immer unangetastet.
Bridge, Routen und Proxy-ARP stellt der Dienst `pombot-network` nach jedem Neustart wieder her.

Für den Bridge-Modus braucht jeder Node eine Linux-Bridge (Standard `vmbr0`). Beim Hinzufügen per SSH kann
sie automatisch angelegt werden (Backup der alten Konfiguration unter `/etc/pombot/network-backup-*`).

### Adminbereich (Einstellungen)

Unter **Einstellungen** (nur für Admins) lässt sich alles ohne Konsole verwalten:

- **Allgemein:** Registrierung, Standard-Kontingente, DNS-Server, Spoofing-Schutz, Auto-Backup-Limit
- **Discord-Login:** Client-ID/Secret, erlaubter Discord-Server, automatische Admins (Redirect-URL zum Kopieren)
- **Cloudflare-DNS:** API-Token hinterlegen, Zonen und DNS-Einträge anlegen/ändern/löschen.
  Im Netzwerk-Tab eines Servers kann ihm per Klick eine Domain zugewiesen werden (A/AAAA auf seine IPs).
- **Domain & HTTPS:** Panel unter eigener Domain (z. B. `panel.deinedomain.de`, Port 443) mit
  **Let's-Encrypt-Zertifikat**. Die Bestätigung läuft über Cloudflare-DNS – Port 80 muss nicht offen sein.
  Das Zertifikat wird automatisch 30 Tage vor Ablauf verlängert.

Cloudflare-Token: dash.cloudflare.com → Mein Profil → API-Token → Vorlage „DNS-Zone bearbeiten“
(`Zone → DNS → Bearbeiten`, `Zone → Zone → Lesen`).

Werte aus dem Adminbereich haben Vorrang vor `/etc/pombot/panel.env`.

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
- **ISO-Bibliothek pro Node:** ISOs im Browser hochladen (in 32-MB-Stücken, auch mehrere GB) oder vom Node
  per URL laden; VMs aus eigener ISO erstellen; ISO in bestehende VMs einlegen/auswerfen, Start von CD
- **Konsole im Browser:** noVNC für VMs, Terminal (xterm.js) für Container, Root-Shell für Nodes (Admin)
- **Ressourcen ändern:** CPU, RAM, Festplatte vergrößern (mit Kontingentprüfung)
- **Netzwerk ändern:** IP tauschen, bestimmte IP wählen, Pool wechseln (geroutet ↔ Bridge), MAC-Adresse setzen;
  feste MAC pro IP im Pool (`77.90.52.70 bc:24:11:11:dc:25`) für Hoster, die IPs an MACs binden
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

## Heartbeat

Jedes Panel meldet 60 Sekunden nach dem Start und danach täglich an `https://vm.errorhunter.it/heartbeats`:
Panel-/Agent-Version, eine zufällige Installations-ID sowie technische Kennzahlen zu Nodes (System, Kernel,
Virtualisierung, CPU/RAM) und Servern (Typ, Vorlage, Ressourcen, Status). Nicht gesendet werden IP-Adressen,
Host- oder Servernamen, Benutzer, E-Mails oder Passwörter; der Empfänger sieht wie bei jeder Verbindung die
öffentliche IP des Panels. Abschalten: `POMBOT_HEARTBEAT=0` in `/etc/pombot/panel.env`, danach
`systemctl restart pombot-panel`.

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
