"""Cloudflare-API: Zonen und DNS-Einträge verwalten.

Benötigter API-Token (dash.cloudflare.com → Mein Profil → API-Token → Token erstellen):
  Zone → DNS → Bearbeiten  und  Zone → Zone → Lesen  (für alle oder ausgewählte Zonen)
"""
import httpx

API = "https://api.cloudflare.com/client/v4"
RECORD_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "SRV", "CAA", "NS", "PTR")


class CloudflareError(Exception):
    pass


class Cloudflare:
    def __init__(self, token: str):
        if not token:
            raise CloudflareError("Kein Cloudflare-API-Token hinterlegt (Einstellungen → Cloudflare)")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _req(self, method: str, path: str, **kwargs):
        try:
            resp = httpx.request(method, API + path, headers=self.headers, timeout=20, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"Cloudflare nicht erreichbar: {exc}")
        try:
            data = resp.json()
        except ValueError:
            raise CloudflareError(f"Unerwartete Antwort von Cloudflare (HTTP {resp.status_code})")
        if not data.get("success"):
            codes = {e.get("code") for e in data.get("errors", [])}
            if resp.status_code in (400, 401, 403) and codes & {1000, 6003, 6111, 9106, 9109, 10000}:
                raise CloudflareError("API-Token ungültig oder ohne Berechtigung – benötigt werden "
                                      "„Zone → DNS → Bearbeiten“ und „Zone → Zone → Lesen“")
            errors = "; ".join(f"{e.get('message')} ({e.get('code')})" for e in data.get("errors", []))
            raise CloudflareError(errors or f"Cloudflare-Fehler (HTTP {resp.status_code})")
        return data

    def verify(self) -> dict:
        zones = self.zones()
        return {"ok": True, "zones": len(zones)}

    def zones(self) -> list[dict]:
        result, page = [], 1
        while True:
            data = self._req("GET", "/zones", params={"per_page": 50, "page": page})
            result += [{"id": z["id"], "name": z["name"], "status": z["status"]} for z in data["result"]]
            info = data.get("result_info") or {}
            if page >= info.get("total_pages", 1):
                return result
            page += 1

    def zone_for(self, name: str) -> dict:
        """Zone, zu der ein Hostname gehört (längste passende Endung)."""
        name = name.rstrip(".").lower()
        matches = [z for z in self.zones() if name == z["name"] or name.endswith("." + z["name"])]
        if not matches:
            raise CloudflareError(f"Keine Cloudflare-Zone für {name} gefunden – ist die Domain im Account?")
        return max(matches, key=lambda z: len(z["name"]))

    def records(self, zone_id: str) -> list[dict]:
        result, page = [], 1
        while True:
            data = self._req("GET", f"/zones/{zone_id}/dns_records", params={"per_page": 500, "page": page})
            result += [{k: r.get(k) for k in ("id", "type", "name", "content", "ttl", "proxied", "priority", "comment")}
                       for r in data["result"]]
            info = data.get("result_info") or {}
            if page >= info.get("total_pages", 1):
                return result
            page += 1

    def find(self, zone_id: str, name: str, rtype: str) -> list[dict]:
        data = self._req("GET", f"/zones/{zone_id}/dns_records", params={"name": name, "type": rtype})
        return data["result"]

    def create(self, zone_id: str, record: dict) -> dict:
        return self._req("POST", f"/zones/{zone_id}/dns_records", json=record)["result"]

    def update(self, zone_id: str, record_id: str, record: dict) -> dict:
        return self._req("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=record)["result"]

    def delete(self, zone_id: str, record_id: str) -> None:
        self._req("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    def upsert(self, name: str, rtype: str, content: str, proxied: bool = False, comment: str = "") -> dict:
        """Legt einen Eintrag an oder aktualisiert den vorhandenen gleichen Namens/Typs."""
        zone = self.zone_for(name)
        record = {"type": rtype, "name": name, "content": content, "ttl": 1, "proxied": proxied}
        if comment:
            record["comment"] = comment[:100]
        existing = self.find(zone["id"], name, rtype)
        if existing:
            return self.update(zone["id"], existing[0]["id"], record)
        return self.create(zone["id"], record)
