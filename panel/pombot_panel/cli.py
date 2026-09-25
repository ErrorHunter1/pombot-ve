"""Kommandozeile: pombot-panel create-admin | reset-password | add-node | list-users"""
import argparse
import sys

from sqlalchemy import func, select

from . import ops
from . import runtime
from .db import SessionLocal, migrate
from .models import User
from .routers.system import seed_templates
from .security import generate_password, hash_password


def main() -> int:
    parser = argparse.ArgumentParser(prog="pombot-panel")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-admin", help="Administrator anlegen")
    p.add_argument("--username", default="admin")
    p.add_argument("--password")

    p = sub.add_parser("reset-password", help="Passwort eines Benutzers zurücksetzen")
    p.add_argument("username")
    p.add_argument("--password")

    p = sub.add_parser("add-node", help="Node per Join-Code hinzufügen")
    p.add_argument("--join", required=True)
    p.add_argument("--name")

    sub.add_parser("list-users", help="Benutzer auflisten")

    args = parser.parse_args()
    migrate()
    with SessionLocal() as db:
        seed_templates(db)
        runtime.load(db)
        if args.cmd == "create-admin":
            if db.scalar(select(User).where(func.lower(User.username) == args.username.lower())):
                print(f"Benutzer {args.username} existiert bereits", file=sys.stderr)
                return 1
            password = args.password or generate_password()
            db.add(User(username=args.username, password_hash=hash_password(password), role="admin",
                        max_guests=1000, max_cores=10000, max_memory_mb=10_000_000, max_disk_gb=1_000_000,
                        max_ips=10000))
            db.commit()
            print(f"Admin angelegt: {args.username} / {password}")
        elif args.cmd == "reset-password":
            user = db.scalar(select(User).where(func.lower(User.username) == args.username.lower()))
            if not user:
                print("Benutzer nicht gefunden", file=sys.stderr)
                return 1
            password = args.password or generate_password()
            user.password_hash = hash_password(password)
            user.active = True
            db.commit()
            print(f"Neues Passwort für {user.username}: {password}")
        elif args.cmd == "add-node":
            data = ops.decode_join(args.join)
            node = ops.register_node(args.name or data.get("name") or data["host"], data["host"],
                                     int(data.get("port", 8007)), data["token"], data["cert"])
            print(f"Node {node.name} hinzugefügt ({data['host']}:{data.get('port', 8007)})")
        elif args.cmd == "list-users":
            for u in db.scalars(select(User).order_by(User.id)):
                print(f"{u.id:4}  {u.username:24} {u.role:6} {'aktiv' if u.active else 'gesperrt'}"
                      f"{'  discord' if u.discord_id else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
