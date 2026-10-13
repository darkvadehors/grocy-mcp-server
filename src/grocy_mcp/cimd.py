"""Récupération des documents de métadonnées client (CIMD) : https, hôtes autorisés, adresses publiques, 3 s, 5 Ko."""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import anyio
import httpx2

MAX_BYTES = 5 * 1024
TIMEOUT = 3.0


class CimdError(Exception):
    pass


class CimdUnavailable(CimdError):
    """Document momentanément injoignable (réseau, DNS, réponse HTTP non 200) — à distinguer d'un document refusé."""


async def _resolve(host: str) -> list[str]:
    infos = await anyio.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class CimdFetcher:
    def __init__(
        self,
        allowed_hosts: tuple[str, ...],
        transport: httpx2.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._hosts = {h.lower() for h in allowed_hosts}
        self._transport = transport
        self._resolve = resolver or _resolve

    async def fetch(self, url: str) -> dict:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
            raise CimdError("adresse de métadonnées refusée")
        if parts.query or parts.fragment or not parts.hostname:
            raise CimdError("adresse de métadonnées refusée")
        host = parts.hostname.lower()
        if host not in self._hosts:
            raise CimdError("hôte de métadonnées non autorisé")
        try:
            addresses = await self._resolve(host)
        except OSError:
            raise CimdUnavailable("résolution DNS impossible") from None
        if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
            raise CimdError("adresse IP interdite pour les métadonnées")
        try:
            async with httpx2.AsyncClient(transport=self._transport, follow_redirects=False, timeout=TIMEOUT) as http:
                async with http.stream("GET", url, headers={"Accept": "application/json"}) as response:
                    if response.status_code != 200:
                        raise CimdUnavailable(f"métadonnées indisponibles ({response.status_code})")
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise CimdError("document de métadonnées trop volumineux")
        except httpx2.HTTPError:
            raise CimdUnavailable("métadonnées injoignables") from None
        try:
            document = json.loads(body)
        except ValueError:
            raise CimdError("métadonnées illisibles") from None
        if not isinstance(document, dict) or document.get("client_id") != url:
            raise CimdError("client_id différent de l'adresse du document")
        return document
