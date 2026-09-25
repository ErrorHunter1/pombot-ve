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
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user
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
    cores: Mapped[int] = mapped_column(Integer)
    memory_mb: Mapped[int] = mapped_column(Integer)
    disk_gb: Mapped[int] = mapped_column(Integer)
    mac: Mapped[str] = mapped_column(String(17))
    status: Mapped[str] = mapped_column(String(16), default="creating")  # creating|ready|busy|error|deleting
    power: Mapped[str] = mapped_column(String(16), default="unknown")  # running|stopped|paused|unknown|missing
    error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    node: Mapped[Node] = relationship(back_populates="guests")
    owner: Mapped[User] = relationship(back_populates="guests")
    template: Mapped[Template | None] = relationship()
    ips: Mapped[list[IPAddress]] = relationship(back_populates="guest")

    @property
    def agent_name(self) -> str:
        return f"pv{self.vmid}"


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
    last_run: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(16))


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
