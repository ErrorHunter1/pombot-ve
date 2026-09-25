"""Let's-Encrypt-Zertifikate (ACME v2) mit DNS-Challenge über Cloudflare.

Vorteil der DNS-Challenge: Port 80 muss nicht offen sein, und das Zertifikat lässt sich auch holen,
während das Panel auf einem anderen Port läuft.
"""
import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.x509.oid import NameOID

from .cloudflare import Cloudflare

PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


class AcmeError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _load_or_create_key(path: Path) -> ec.EllipticCurvePrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return key


class Acme:
    def __init__(self, directory_url: str, account_key: ec.EllipticCurvePrivateKey, log):
        self.http = httpx.Client(timeout=30, headers={"User-Agent": "pombot-ve"})
        self.dir = self.http.get(directory_url).json()
        self.key = account_key
        self.kid = None
        self.log = log

    def _jwk(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {"crv": "P-256", "kty": "EC", "x": _b64(numbers.x.to_bytes(32, "big")),
                "y": _b64(numbers.y.to_bytes(32, "big"))}

    def thumbprint(self) -> str:
        canonical = json.dumps(self._jwk(), sort_keys=True, separators=(",", ":")).encode()
        return _b64(hashlib.sha256(canonical).digest())

    def _nonce(self) -> str:
        return self.http.head(self.dir["newNonce"]).headers["Replay-Nonce"]

    def post(self, url: str, payload) -> httpx.Response:
        """Signierte Anfrage (payload=None für POST-as-GET)."""
        for _ in range(3):
            protected = {"alg": "ES256", "nonce": self._nonce(), "url": url}
            protected.update({"kid": self.kid} if self.kid else {"jwk": self._jwk()})
            p64 = _b64(json.dumps(protected).encode())
            body64 = "" if payload is None else _b64(json.dumps(payload).encode())
            der = self.key.sign(f"{p64}.{body64}".encode(), ec.ECDSA(hashes.SHA256()))
            r, s = utils.decode_dss_signature(der)
            sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
            resp = self.http.post(url, content=json.dumps({"protected": p64, "payload": body64, "signature": sig}),
                                  headers={"Content-Type": "application/jose+json"})
            if resp.status_code == 400 and "badNonce" in resp.text:
                continue
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("detail", resp.text)
                except ValueError:
                    detail = resp.text
                raise AcmeError(f"Let's Encrypt: {detail}")
            return resp
        raise AcmeError("Let's Encrypt: wiederholt ungültige Nonce")

    def register(self, email: str) -> None:
        payload = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = [f"mailto:{email}"]
        self.kid = self.post(self.dir["newAccount"], payload).headers["Location"]

    def poll(self, url: str, wanted: str, timeout: int = 180) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.post(url, None).json()
            if data["status"] == wanted:
                return data
            if data["status"] == "invalid":
                errors = [c.get("error", {}).get("detail") for c in data.get("challenges", []) if c.get("error")]
                raise AcmeError("Prüfung fehlgeschlagen: " + ("; ".join(e for e in errors if e) or json.dumps(data)))
            time.sleep(3)
        raise AcmeError("Zeitüberschreitung bei Let's Encrypt")


def _wait_txt(name: str, value: str, log, timeout: int = 180) -> None:
    """Wartet, bis der TXT-Eintrag öffentlich sichtbar ist (Abfrage über DNS-over-HTTPS)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for resolver in ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"):
            try:
                resp = httpx.get(resolver, params={"name": name, "type": "TXT"},
                                 headers={"Accept": "application/dns-json"}, timeout=10)
                answers = [a.get("data", "").strip('"') for a in resp.json().get("Answer", [])]
                if value in answers:
                    log(f"TXT-Eintrag ist sichtbar ({resolver.split('/')[2]}).")
                    return
            except (httpx.HTTPError, ValueError):
                pass
        time.sleep(5)
    log("TXT-Eintrag noch nicht überall sichtbar – versuche es trotzdem.")


def issue(domain: str, email: str, cf: Cloudflare, tls_dir: Path, log, staging: bool = False) -> dict:
    """Holt ein Zertifikat für `domain` und speichert fullchain.pem/privkey.pem in `tls_dir`."""
    tls_dir.mkdir(parents=True, exist_ok=True)
    account_key = _load_or_create_key(tls_dir / ("account-staging.pem" if staging else "account.pem"))
    acme = Acme(STAGING if staging else PRODUCTION, account_key, log)
    log(f"Registriere bei Let's Encrypt{' (Testumgebung)' if staging else ''} …")
    acme.register(email)
    resp = acme.post(acme.dir["newOrder"], {"identifiers": [{"type": "dns", "value": domain}]})
    order_url, order = resp.headers["Location"], resp.json()
    for authz_url in order["authorizations"]:
        authz = acme.post(authz_url, None).json()
        if authz["status"] == "valid":
            continue
        challenge = next(c for c in authz["challenges"] if c["type"] == "dns-01")
        txt_value = _b64(hashlib.sha256(f"{challenge['token']}.{acme.thumbprint()}".encode()).digest())
        txt_name = f"_acme-challenge.{authz['identifier']['value']}"
        zone = cf.zone_for(txt_name)
        log(f"Lege TXT-Eintrag {txt_name} in Cloudflare-Zone {zone['name']} an …")
        record = cf.create(zone["id"], {"type": "TXT", "name": txt_name, "content": f'"{txt_value}"', "ttl": 60,
                                        "comment": "PomBot VE: Let's-Encrypt-Prüfung (temporär)"})
        try:
            _wait_txt(txt_name, txt_value, log)
            time.sleep(10)
            log("Starte Prüfung durch Let's Encrypt …")
            acme.post(challenge["url"], {})
            acme.poll(authz_url, "valid")
            log("Domain bestätigt.")
        finally:
            try:
                cf.delete(zone["id"], record["id"])
            except Exception:  # noqa: BLE001
                log("Hinweis: temporärer TXT-Eintrag konnte nicht gelöscht werden.")
    cert_key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
           .sign(cert_key, hashes.SHA256()))
    log("Fordere Zertifikat an …")
    acme.post(order["finalize"], {"csr": _b64(csr.public_bytes(serialization.Encoding.DER))})
    order = acme.poll(order_url, "valid")
    chain = acme.post(order["certificate"], None).text
    (tls_dir / "privkey.pem").write_bytes(cert_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (tls_dir / "privkey.pem").chmod(0o600)
    (tls_dir / "fullchain.pem").write_text(chain)
    info = cert_info(tls_dir / "fullchain.pem")
    log(f"Zertifikat gespeichert, gültig bis {info['not_after']}.")
    return info


def cert_info(path: Path) -> dict | None:
    if not path.exists():
        return None
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else \
        cert.not_valid_after.replace(tzinfo=timezone.utc)
    names = []
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        pass
    return {"domains": names, "issuer": cert.issuer.rfc4514_string(), "not_after": not_after.isoformat(),
            "days_left": (not_after - datetime.now(timezone.utc)).days}
