"""Deutsche Kurzbeschreibungen für die REST-API-Dokumentation (Schlüssel = Funktionsname des Endpunkts).

Längere Hinweise stehen als Docstring direkt am Endpunkt oder hier unter NOTES.
"""

SUMMARY = {
    # Anmeldung
    "auth_config": "Anmeldeoptionen und Version", "login": "Im Panel anmelden (Sitzung)", "logout": "Abmelden",
    # Server
    "list_guests": "Server auflisten", "create_guest": "Server erstellen", "get_guest": "Server abrufen",
    "patch_guest": "Server ändern (Name, Notizen, Besitzer, HA, Autostart, Löschschutz, Tags)",
    "delete_guest": "Server löschen", "guest_status": "Live-Status (CPU, RAM, Netz, Festplatte)",
    "guest_action": "Starten, stoppen, herunterfahren, neu starten, pausieren, fortsetzen",
    "resize_guest": "CPU, RAM und Festplatte ändern", "reset_password": "root-Passwort neu setzen",
    "change_network": "IP-Adressen und MAC ändern", "get_cdrom": "CD-Laufwerk abfragen", "set_cdrom": "ISO einlegen/auswerfen",
    "migrate_check": "Umzug prüfen (nutzbare IPs auf dem Ziel)", "migrate_guest": "Auf anderen Node umziehen",
    "guest_app": "Status der mitinstallierten Anwendung", "clone_guest": "Server klonen",
    "reinstall_guest": "Neu installieren (optional mit Anwendung)",
    "list_snapshots": "Snapshots auflisten", "create_snapshot": "Snapshot erstellen", "delete_snapshot": "Snapshot löschen",
    "rollback_snapshot": "Snapshot zurückspielen", "list_backups": "Backups auflisten", "create_backup": "Backup erstellen",
    "delete_backup": "Backup löschen", "restore_backup": "Backup wiederherstellen",
    "get_firewall": "Firewall abrufen", "put_firewall": "Firewall setzen",
    "get_schedule": "Backup-Zeitplan abrufen", "put_schedule": "Backup-Zeitplan setzen",
    # Nodes
    "list_nodes": "Nodes auflisten", "get_node": "Node abrufen", "patch_node": "Node ändern", "delete_node": "Node entfernen",
    "add_node_ssh": "Node per SSH installieren und hinzufügen", "add_node_join": "Node per Join-Code hinzufügen",
    "refresh_node": "Node-Informationen neu einlesen", "node_images": "Cloud-Images im Cache",
    "node_image_delete": "Cloud-Image löschen", "node_network": "Erkannte IPs und Gateways des Nodes",
    "node_isos": "ISOs des Nodes", "node_iso_delete": "ISO löschen", "node_iso_upload_start": "ISO-Upload beginnen",
    "node_iso_upload_status": "ISO-Upload: Stand", "node_iso_upload_chunk": "ISO-Upload: Stück senden",
    "node_iso_upload_abort": "ISO-Upload abbrechen", "node_iso_upload_finish": "ISO-Upload abschließen",
    "node_iso_fetch": "ISO per URL auf den Node laden", "node_backups": "Backups auf dem Node",
    # IP-Pools
    "list_pools": "IP-Pools auflisten", "create_pool": "IP-Pool anlegen", "update_pool": "IP-Pool ändern",
    "delete_pool": "IP-Pool löschen", "pool_addresses": "Vergebene/reservierte Adressen", "free_addresses": "Freie Adressen",
    "reserve_address": "Adresse reservieren", "release_address": "Reservierung aufheben",
    # Benutzer
    "me": "Eigenes Konto mit Kontingent", "update_me": "Eigene SSH-Schlüssel ändern", "change_password": "Eigenes Passwort ändern",
    "unlink_discord": "Discord-Verknüpfung lösen", "list_users": "Benutzer auflisten", "create_user": "Benutzer anlegen",
    "list_users_brief": "Benutzer (Kurzliste)", "update_user": "Benutzer ändern (Rolle, Kontingent, Sperre, Passwort)",
    "delete_user": "Benutzer löschen",
    # System
    "list_templates": "Vorlagen auflisten", "create_template": "Vorlage anlegen", "update_template": "Vorlage ändern",
    "delete_template": "Vorlage löschen", "list_tasks": "Aufgaben auflisten", "get_task": "Aufgabe mit Log abrufen",
    "audit_log": "Audit-Protokoll", "dashboard": "Übersicht (Zahlen für das Dashboard)",
    "list_apps": "Anwendungs-Katalog",
    # Speicher & Backup-Ziele
    "list_storages": "Gemeinsame Speicher auflisten", "create_storage": "Gemeinsamen Speicher anlegen",
    "update_storage": "Gemeinsamen Speicher ändern", "delete_storage": "Gemeinsamen Speicher entfernen",
    "sync_storages": "Speicher auf allen Nodes einhängen", "storages_for_create": "Wählbare Speicher beim Erstellen",
    "list_targets": "Backup-Speicher auflisten", "create_target": "Backup-Speicher anlegen", "update_target": "Backup-Speicher ändern",
    "delete_target": "Backup-Speicher entfernen", "test_target": "Backup-Speicher testen",
    # Updates, Branding, Einstellungen
    "update_info": "Versionen und Update-Status", "update_check": "Jetzt nach Updates sehen",
    "update_start": "Update starten (Panel, Nodes oder beides)", "update_node": "Einzelnen Node aktualisieren",
    "branding_get": "Branding & SEO abrufen", "branding_put": "Branding & SEO speichern", "branding_upload": "Bild hochladen",
    "branding_delete": "Bild entfernen", "get_settings": "Einstellungen abrufen", "put_settings": "Einstellungen speichern",
    "cf_verify": "Cloudflare-Token prüfen", "cf_zones": "Cloudflare-Zonen", "cf_records": "DNS-Einträge einer Zone",
    "cf_create": "DNS-Eintrag anlegen", "cf_update": "DNS-Eintrag ändern", "cf_delete": "DNS-Eintrag löschen",
    "guest_dns": "DNS-Einträge für einen Server setzen", "get_domain": "Domain & HTTPS abrufen",
    "set_domain": "Domain mit Zertifikat einrichten", "reset_domain": "Domain entfernen (zurück zur IP)",
    "export_config": "Konfiguration exportieren (JSON)", "export_sections": "Exportierbare Bereiche",
}

NOTES = {
    "create_guest": "Legt einen Server an und liefert `guest_id`, `task_id` und das root-Passwort. Pflicht: `name`, `hostname`, "
                    "`cores`, `memory_mb`, `disk_gb` und `template_id` (Vorlagen: `GET /api/templates`) oder `iso_file`. "
                    "`node`, `ipv4_pool`, `ipv6_pool`: `auto`, `none` oder eine ID. Anwendung mitinstallieren: `app_id` aus "
                    "`GET /api/apps` und `app_params` (Umgebungsvariablen des Katalogs, z. B. `{\"APP_EMAIL\": \"…\"}`). "
                    "Fortschritt über `GET /api/tasks/{task_id}`.",
    "guest_action": "`action`: `start`, `stop` (hart), `shutdown`, `reboot`, `suspend`, `resume`. Antwort: neuer Zustand.",
    "list_guests": "Mit `?all=true` alle Server (nur Administratoren), sonst nur die eigenen.",
    "get_task": "`status`: `running`, `ok` oder `error`; `log` enthält die Ausgabe.",
    "update_start": "`scope`: `all` (Panel + Nodes), `panel` oder `nodes`; `node_ids` optional (sonst alle Nodes).",
    "clone_guest": "Vollständige Kopie auf demselben Node mit neuer IP, neuem Hostnamen und neuem Passwort (Antwort wie beim Erstellen).",
}


def apply(spec: dict) -> None:
    """Setzt deutsche Titel/Beschreibungen in eine OpenAPI-Beschreibung ein."""
    for item in spec.get("paths", {}).values():
        for op in item.values():
            if not isinstance(op, dict) or "operationId" not in op:
                continue
            name = op["operationId"].split("_api_")[0]
            if name in SUMMARY:
                op["summary"] = SUMMARY[name]
            if name in NOTES:
                op["description"] = NOTES[name] + ("\n\n" + op["description"] if op.get("description") else "")
