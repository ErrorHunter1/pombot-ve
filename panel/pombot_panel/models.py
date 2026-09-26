import json
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(255))
    discord_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    avatar_url: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # Role.key: admin | user | eigene Rollen
    login_methods: Mapped[str] = mapped_column(String(10), default="any")  # any | password | discord
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_guests: Mapped[int] = mapped_column(Integer, default=2)
    max_cores: Mapped[int] = mapped_column(Integer, default=4)
    max_memory_mb: Mapped[int] = mapped_column(Integer, default=4096)
    max_disk_gb: Mapped[int] = mapped_column(Integer, default=50)
    max_ips: Mapped[int] = mapped_column(Integer, default=2)
    ssh_keys: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)

    guests: Mapped[list["Guest"]] = relationship(back_populates="owner")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can(self, perm: str) -> bool:
        """Hat der Benutzer dieses Recht (über seine Rolle)? Siehe perms.py."""
        from .perms import can
        return can(self, perm)


class Node(Base):
    __tablename__ = "nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=8007)
    token: Mapped[str] = mapped_column(String(128))
    cert_pem: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # online | offline | unknown
    info_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # für automatische Platzierung
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    guests: Mapped[list["Guest"]] = relationship(back_populates="node")

    @property
    def info(self) -> dict:
        try:
            return json.loads(self.info_json or "{}")
        except ValueError:
            return {}


class IPPool(Base):
    __tablename__ = "ip_pools"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    network: Mapped[str] = mapped_column(String(64), default="")  # z. B. 203.0.113.0/24 (bei Listen optional)
    mode: Mapped[str] = mapped_column(String(8), default="bridged")  # bridged | routed
    address_list: Mapped[str] = mapped_column(Text, default="")  # einzelne Adressen statt Bereich
    gateway: Mapped[str | None] = mapped_column(String(64))
    dns: Mapped[str] = mapped_column(String(255), default="")
    bridge: Mapped[str] = mapped_column(String(32), default="vmbr0")
    range_start: Mapped[str | None] = mapped_column(String(64))
    range_end: Mapped[str | None] = mapped_column(String(64))
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"))
    admin_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    addresses: Mapped[list["IPAddress"]] = relationship(back_populates="pool", cascade="all, delete-orphan")
    node: Mapped["Node"] = relationship()


class IPAddress(Base):
    __tablename__ = "ip_addresses"
    __table_args__ = (UniqueConstraint("pool_id", "address"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("ip_pools.id", ondelete="CASCADE"))
    address: Mapped[str] = mapped_column(String(64))
    guest_id: Mapped[int | None] = mapped_column(ForeignKey("guests.id", ondelete="SET NULL"))
    reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    pool: Mapped[IPPool] = relationship(back_populates="addresses")
    guest: Mapped["Guest"] = relationship(back_populates="ips")


class Template(Base):
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(8))  # kvm | lxc
    source: Mapped[str] = mapped_column(String(8))  # cloud | iso | lxc
    url: Mapped[str | None] = mapped_column(String(512))
    lxc_dist: Mapped[str | None] = mapped_column(String(32))
    lxc_release: Mapped[str | None] = mapped_column(String(32))
    os_family: Mapped[str] = mapped_column(String(16), default="linux")  # debian | ubuntu | windows | linux
    min_disk_gb: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)


class Guest(Base):
    __tablename__ = "guests"
    __table_args__ = {"sqlite_autoincrement": True}  # IDs gelöschter Server nie wiederverwenden
    id: Mapped[int] = mapped_column(primary_key=True)
    vmid: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(64))
    hostname: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(8))  # kvm | lxc
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id", ondelete="SET NULL"))
    iso_file: Mapped[str | None] = mapped_column(String(160))  # VM aus eigener ISO (Bibliothek des Nodes)
    cores: Mapped[int] = mapped_column(Integer)
    memory_mb: Mapped[int] = mapped_column(Integer)
    disk_gb: Mapped[int] = mapped_column(Integer)
    mac: Mapped[str] = mapped_column(String(17))
    status: Mapped[str] = mapped_column(String(16), default="creating")  # creating|ready|busy|error|deleting
    power: Mapped[str] = mapped_column(String(16), default="unknown")  # running|stopped|paused|unknown|missing
    error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="")
    storage_id: Mapped[int | None] = mapped_column(ForeignKey("storages.id"))  # gemeinsamer Speicher (None = lokal)
    ha: Mapped[bool] = mapped_column(Boolean, default=False)  # bei Node-Ausfall auf anderem Node starten
    app_id: Mapped[str | None] = mapped_column(String(40))  # mitinstallierte Anwendung (apps.py)
    app_params: Mapped[str] = mapped_column(Text, default="")  # Eingaben dazu (JSON)
    onboot: Mapped[bool] = mapped_column(Boolean, default=True)  # beim Start des Nodes mitstarten
    protected: Mapped[bool] = mapped_column(Boolean, default=False)  # Löschschutz (kein Löschen/Neuinstallieren)
    tags: Mapped[str] = mapped_column(String(255), default="")  # Komma-getrennt, z. B. "web,kunde-a"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    node: Mapped[Node] = relationship(back_populates="guests")
    storage: Mapped["SharedStorage | None"] = relationship()
    owner: Mapped[User] = relationship(back_populates="guests")
    template: Mapped[Template | None] = relationship()
    ips: Mapped[list[IPAddress]] = relationship(back_populates="guest")

    @property
    def agent_name(self) -> str:
        return f"pv{self.vmid}"


class SharedStorage(Base):
    """Gemeinsamer Speicher (NFS, SMB oder vorhandener Mount wie CephFS) für Server-Festplatten.
    Wird auf allen Nodes eingehängt – Grundlage für HA und schnelle Migration."""
    __tablename__ = "storages"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(8))  # nfs | smb | path
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    user_visible: Mapped[bool] = mapped_column(Boolean, default=False)  # auch Benutzer dürfen Server hier anlegen
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def config(self) -> dict:
        try:
            return json.loads(self.config_json or "{}")
        except ValueError:
            return {}


class FirewallConfig(Base):
    """Firewall eines Servers. Regeln als JSON-Liste (siehe routers/extras.py)."""
    __tablename__ = "firewalls"
    guest_id: Mapped[int] = mapped_column(ForeignKey("guests.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_in: Mapped[str] = mapped_column(String(8), default="drop")
    policy_out: Mapped[str] = mapped_column(String(8), default="accept")
    antispoof: Mapped[bool] = mapped_column(Boolean, default=True)
    rules_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def rules(self) -> list[dict]:
        try:
            return json.loads(self.rules_json or "[]")
        except ValueError:
            return []


class BackupTarget(Base):
    """Externer Backup-Speicher (SFTP, S3, NFS, SMB). Zugangsdaten stehen in config_json."""
    __tablename__ = "backup_targets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(8))  # sftp | s3 | nfs | smb
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    user_visible: Mapped[bool] = mapped_column(Boolean, default=False)  # auch Benutzer dürfen hierhin sichern
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def config(self) -> dict:
        try:
            return json.loads(self.config_json or "{}")
        except ValueError:
            return {}


class BackupSchedule(Base):
    """Zeitgesteuerte Backups eines Servers (Uhrzeit = Serverzeit des Panels)."""
    __tablename__ = "backup_schedules"
    guest_id: Mapped[int] = mapped_column(ForeignKey("guests.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency: Mapped[str] = mapped_column(String(8), default="daily")  # daily | weekly
    weekday: Mapped[int] = mapped_column(Integer, default=6)  # 0 = Montag … 6 = Sonntag
    hour: Mapped[int] = mapped_column(Integer, default=3)
    minute: Mapped[int] = mapped_column(Integer, default=0)
    keep: Mapped[int] = mapped_column(Integer, default=7)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("backup_targets.id", ondelete="SET NULL"))
    keep_local: Mapped[bool] = mapped_column(Boolean, default=False)  # lokale Kopie nach dem Hochladen behalten
    last_run: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(16))


class Role(Base):
    """Rolle mit einzelnen Rechten (siehe perms.PERMISSIONS). admin und user sind eingebaut."""
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(16), unique=True)  # steht in User.role
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), default="")
    permissions: Mapped[str] = mapped_column(Text, default="[]")  # JSON-Liste
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Invite(Base):
    """Einladung: Link, mit dem sich jemand selbst ein Konto mit vorgegebener Rolle und Anmeldeart anlegt."""
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(12))
    email: Mapped[str | None] = mapped_column(String(255))  # nur zur Info / Vorbelegung
    note: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")
    login_methods: Mapped[str] = mapped_column(String(10), default="any")  # any | password | discord
    quota_json: Mapped[str] = mapped_column(Text, default="{}")  # max_guests, max_cores … (leer = Standard)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    @property
    def quota(self) -> dict:
        try:
            return json.loads(self.quota_json or "{}")
        except ValueError:
            return {}


class ApiToken(Base):
    """Token für die REST-API (nur Hash gespeichert). Handelt mit den Rechten des Admins, der es angelegt hat."""
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # Anfang des Tokens zum Wiedererkennen
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Setting(Base):
    """Einfache Schlüssel/Wert-Einstellungen (z. B. Zähler für VMIDs)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    node_id: Mapped[int | None] = mapped_column(Integer)
    guest_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error
    log: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
