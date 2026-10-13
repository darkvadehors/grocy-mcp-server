"""Politique d'enregistrement des clients OAuth (anti-hameçonnage du consentement, spec §3.2-3.3 et §4.2)."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from pydantic import AnyUrl

from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

_CLAUDE = {"https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"}
_CHATGPT = {"https://chatgpt.com/connector_platform_oauth_redirect"}
_CHATGPT_CALLBACK = re.compile(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PROVIDER_WORDS = {"claude": "claude", "anthropic": "claude", "chatgpt": "chatgpt", "openai": "chatgpt"}
# Lettres latines imitées par des caractères d'autres alphabets (le cyrillique « С » de « Сlaude », etc.).
_CONFUSABLES = str.maketrans({"а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "і": "i", "ӏ": "l", "υ": "u", "ο": "o"})


def is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return (
        parts.scheme == "http"
        and parts.hostname in _LOOPBACK_HOSTS
        and not parts.query
        and not parts.fragment
        and not parts.username
    )


def classify_redirect(uri: str, extra: tuple[str, ...] = ()) -> str | None:
    if uri in _CLAUDE:
        return "claude"
    if uri in _CHATGPT or _CHATGPT_CALLBACK.fullmatch(uri):
        return "chatgpt"
    if is_loopback(uri):
        return "loopback"
    if uri in extra:
        return "extra"
    return None


def claimed_provider(name: str | None) -> str | None:
    if not name:
        return None
    folded = unicodedata.normalize("NFKC", name).casefold().translate(_CONFUSABLES)
    for word, provider in _PROVIDER_WORDS.items():
        if word in folded:
            return provider
    return None


def display_host(uri: str) -> str:
    if is_loopback(uri):
        return "une application locale sur cet appareil"
    return urlsplit(uri).hostname or "?"


def origin(uri: str) -> str:
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


def clean_name(name: str | None) -> str:
    if not name:
        return "Application sans nom"
    kept = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    return kept.strip()[:60] or "Application sans nom"


class LoopbackAwareClient(OAuthClientInformationFull):
    """Client dont les URI de boucle locale acceptent n'importe quel port (RFC 8252 §7.3), à hôte et chemin égaux."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None and is_loopback(str(redirect_uri)):
            wanted = urlsplit(str(redirect_uri))
            for registered in self.redirect_uris or []:
                reg = urlsplit(str(registered))
                if is_loopback(str(registered)) and reg.hostname == wanted.hostname and (reg.path or "/") == (wanted.path or "/"):
                    return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


class ClientPolicy:
    def __init__(self, extra_redirect_uris: tuple[str, ...]) -> None:
        self._extra = extra_redirect_uris

    def check(self, info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in info.redirect_uris or []]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "au moins une URI de retour est requise")
        kinds = set()
        for uri in uris:
            kind = classify_redirect(uri, self._extra)
            if kind is None:
                raise RegistrationError("invalid_redirect_uri", "URI de retour non autorisée par ce serveur")
            kinds.add(kind)
        provider = claimed_provider(info.client_name)
        if provider and not kinds <= {provider, "loopback"}:
            raise RegistrationError("invalid_client_metadata", "ce nom de client est réservé à son fournisseur officiel")
