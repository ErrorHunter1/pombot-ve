"""HTTPS-Client für die Agents. Das Zertifikat jedes Agents wird fest hinterlegt (Pinning)."""
import hashlib
import ssl

import httpx

from .models import Node


class AgentError(Exception):
    pass


_CTX_CACHE: dict[str, ssl.SSLContext] = {}


def ssl_context(cert_pem: str) -> ssl.SSLContext:
    key = hashlib.sha256(cert_pem.encode()).hexdigest()
    ctx = _CTX_CACHE.get(key)
    if ctx is None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False  # Vertrauen entsteht durch das gepinnte Zertifikat, nicht durch den Namen
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=cert_pem)
        _CTX_CACHE[key] = ctx
    return ctx


def cert_fingerprint(cert_pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert_pem)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        detail = data.get("detail", data)
        if isinstance(detail, list):
            return "; ".join(str(d.get("msg", d)) for d in detail)
        return str(detail)
    except ValueError:
        return resp.text[:500] or f"HTTP {resp.status_code}"


class AgentClient:
    def __init__(self, host: str, port: int, token: str, cert_pem: str):
        host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.base = f"https://{host_part}:{port}"
        self.ws_base = f"wss://{host_part}:{port}"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.ctx = ssl_context(cert_pem)

    @classmethod
    def for_node(cls, node: Node) -> "AgentClient":
        return cls(node.host, node.port, node.token, node.cert_pem)

    def request(self, method: str, path: str, json=None, timeout: float = 30):
        try:
            resp = httpx.request(method, self.base + path, json=json, headers=self.headers,
                                 verify=self.ctx, timeout=timeout)
        except httpx.HTTPError as exc:
            raise AgentError(f"Node nicht erreichbar: {exc}") from exc
        if resp.status_code >= 400:
            raise AgentError(_detail(resp))
        return resp.json()

    async def arequest(self, method: str, path: str, json=None, timeout: float = 30):
        try:
            async with httpx.AsyncClient(verify=self.ctx, timeout=timeout) as client:
                resp = await client.request(method, self.base + path, json=json, headers=self.headers)
        except httpx.HTTPError as exc:
            raise AgentError(f"Node nicht erreichbar: {exc}") from exc
        if resp.status_code >= 400:
            raise AgentError(_detail(resp))
        return resp.json()
