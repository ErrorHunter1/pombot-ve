"""Panel-Konfiguration aus /etc/pombot/panel.env bzw. Umgebungsvariablen."""
import os
from pathlib import Path


def _load_env(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# runtime.env (vom Adminbereich geschrieben: Domain, Port, Zertifikat) hat Vorrang vor panel.env
_load_env(os.path.join(os.environ.get("POMBOT_DATA_DIR", "/var/lib/pombot-panel"), "runtime.env"))
_load_env(os.environ.get("POMBOT_PANEL_ENV", "/etc/pombot/panel.env"))


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Settings:
    version = "0.4.2"
    secret_key = os.environ.get("POMBOT_SECRET_KEY", "")
    db_url = os.environ.get("POMBOT_DB_URL", "sqlite:///./pombot-panel.db")
    base_url = os.environ.get("POMBOT_BASE_URL", "http://localhost:8443").rstrip("/")
    agent_src = os.environ.get("POMBOT_AGENT_SRC", str(Path(__file__).resolve().parents[2] / "agent"))
    poll_interval = _int("POMBOT_POLL_INTERVAL", 10)
    data_dir = os.environ.get("POMBOT_DATA_DIR", "/var/lib/pombot-panel" if os.path.isdir("/var/lib/pombot-panel")
                              else os.path.abspath("./data"))
    port = _int("POMBOT_PORT", 8443)

    # Cloudflare & Let's Encrypt (werden im Adminbereich gesetzt)
    cloudflare_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    acme_email = os.environ.get("POMBOT_ACME_EMAIL", "")
    panel_domain = os.environ.get("POMBOT_PANEL_DOMAIN", "")
    acme_enabled = False
    acme_staging = False

    # Discord OAuth2
    discord_client_id = os.environ.get("DISCORD_CLIENT_ID", "")
    discord_client_secret = os.environ.get("DISCORD_CLIENT_SECRET", "")
    discord_guild_id = os.environ.get("DISCORD_GUILD_ID", "")  # optional: nur Mitglieder dieses Servers
    discord_admin_ids = {x.strip() for x in os.environ.get("DISCORD_ADMIN_IDS", "").split(",") if x.strip()}

    # open = jeder Discord-Nutzer darf rein, approval = Admin muss freischalten, closed = nur bekannte Nutzer
    registration = os.environ.get("POMBOT_REGISTRATION", "approval")

    # Standard-Kontingent für neue Benutzer
    default_max_guests = _int("POMBOT_QUOTA_GUESTS", 2)
    default_max_cores = _int("POMBOT_QUOTA_CORES", 4)
    default_max_memory_mb = _int("POMBOT_QUOTA_MEMORY_MB", 4096)
    default_max_disk_gb = _int("POMBOT_QUOTA_DISK_GB", 50)
    default_max_ips = _int("POMBOT_QUOTA_IPS", 2)

    default_dns = os.environ.get("POMBOT_DEFAULT_DNS", "1.1.1.1,8.8.8.8")

    # Spoofing-Schutz: Server dürfen nur mit ihren eigenen IPs senden (empfohlen bei mehreren Kunden)
    antispoof = os.environ.get("POMBOT_ANTISPOOF", "1") == "1"
    # Maximal aufbewahrte automatische Backups pro Server (für normale Benutzer)
    max_auto_backups = _int("POMBOT_MAX_AUTO_BACKUPS", 7)

    @property
    def https(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_client_id and self.discord_client_secret)

    @property
    def discord_redirect_uri(self) -> str:
        return f"{self.base_url}/api/auth/discord/callback"


settings = Settings()
if not settings.secret_key:
    # Entwicklungsmodus: flüchtiger Schlüssel (Sessions gehen bei Neustart verloren)
    settings.secret_key = os.urandom(32).hex()
