"""Configuration du service, lue au démarrage dans l'environnement (fourni par systemd)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from grocy_mcp.nutrition import parse_household


class ConfigError(Exception):
    """Configuration absente ou invalide."""


@dataclass(frozen=True)
class Settings:
    grocy_base_url: str
    public_url: str
    db_path: Path
    audit_path: Path
    key_path: Path
    max_quantity: int = 50
    inventory_abs_threshold: int = 10
    inventory_pct_threshold: int = 50
    actions_per_minute: int = 30
    actions_per_hour: int = 300
    max_clients: int = 200
    max_connections_per_account: int = 10
    cimd_hosts: tuple[str, ...] = ("claude.ai", "chatgpt.com")
    extra_redirect_uris: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ("https://claude.ai", "https://claude.com", "https://chatgpt.com")
    timezone: str = "UTC"  # fuseau des personnes (dates de péremption « du jour »)
    household: tuple[tuple[str, int], ...] = ()  # foyer pour l'autonomie alimentaire, ex. (("H", 50), ("F", 54))
    usda_key_path: Path | None = None  # clé FoodData Central (facultative)

    @property
    def resource_url(self) -> str:
        return self.public_url + "/mcp"

    @property
    def host(self) -> str:
        return urlsplit(self.public_url).netloc


_INTEGERS = {
    "MCP_MAX_QUANTITY": "max_quantity",
    "MCP_INVENTORY_ABS_THRESHOLD": "inventory_abs_threshold",
    "MCP_INVENTORY_PCT_THRESHOLD": "inventory_pct_threshold",
    "MCP_ACTIONS_PER_MINUTE": "actions_per_minute",
    "MCP_ACTIONS_PER_HOUR": "actions_per_hour",
    "MCP_MAX_CLIENTS": "max_clients",
    "MCP_MAX_CONNECTIONS_PER_ACCOUNT": "max_connections_per_account",
}
_LISTS = {
    "MCP_CIMD_HOSTS": "cimd_hosts",
    "MCP_EXTRA_REDIRECT_URIS": "extra_redirect_uris",
    "MCP_ALLOWED_ORIGINS": "allowed_origins",
}


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"variable manquante : {name}")
    return value


def _bare_https(env: Mapping[str, str], name: str) -> str:
    value = _required(env, name).rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError(f"{name} doit être une adresse https")
    if parts.path or parts.query or parts.fragment:
        raise ConfigError(f"{name} : ni chemin, ni paramètres")
    return value


def _positive_int(env: Mapping[str, str], name: str) -> int | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} doit être un entier") from None
    if value <= 0:
        raise ConfigError(f"{name} doit être strictement positif")
    return value


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    if env.get("MCP_KEY_FILE", "").strip():
        key_path = Path(env["MCP_KEY_FILE"].strip())
    elif env.get("CREDENTIALS_DIRECTORY", "").strip():
        key_path = Path(env["CREDENTIALS_DIRECTORY"].strip()) / "mcp-key"
    else:
        raise ConfigError("clé de chiffrement introuvable : CREDENTIALS_DIRECTORY ou MCP_KEY_FILE")

    options: dict[str, object] = {}
    for name, field in _INTEGERS.items():
        value = _positive_int(env, name)
        if value is not None:
            options[field] = value
    for name, field in _LISTS.items():
        raw = env.get(name, "").strip()
        if raw:
            options[field] = tuple(raw.split())

    zone = env.get("MCP_TIMEZONE", "").strip()
    if zone:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError("MCP_TIMEZONE : fuseau horaire inconnu") from None
        options["timezone"] = zone

    if env.get("MCP_HOUSEHOLD", "").strip():
        try:
            options["household"] = tuple(parse_household(env["MCP_HOUSEHOLD"]))
        except ValueError as error:
            raise ConfigError(str(error)) from None
    if env.get("MCP_USDA_KEY_FILE", "").strip():
        options["usda_key_path"] = Path(env["MCP_USDA_KEY_FILE"].strip())

    return Settings(
        grocy_base_url=_bare_https(env, "GROCY_BASE_URL"),
        public_url=_bare_https(env, "MCP_PUBLIC_URL"),
        db_path=Path(_required(env, "MCP_DB_PATH")),
        audit_path=Path(_required(env, "MCP_AUDIT_PATH")),
        key_path=key_path,
        **options,  # type: ignore[arg-type]
    )
