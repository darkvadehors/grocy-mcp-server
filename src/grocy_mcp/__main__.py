"""Point d'entrée : python -m grocy_mcp serve | generate-key FICHIER | rotate-key ANCIENNE NOUVELLE | conditionner."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

SD_LISTEN_FDS_START = 3


def systemd_socket() -> socket.socket | None:
    """Socket transmise par systemd (grocy-mcp.socket), ou None hors activation par socket."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    if os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("systemd doit transmettre exactement une socket")
    return socket.socket(fileno=SD_LISTEN_FDS_START)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from grocy_mcp.app import build_app
    from grocy_mcp.audit import AuditLog
    from grocy_mcp.config import load_settings
    from grocy_mcp.crypto import SecretBox, load_key
    from grocy_mcp.redact import install_redaction
    from grocy_mcp.store import Store

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    install_redaction()
    sock = systemd_socket()
    if sock is None and not args.uds:
        raise SystemExit("aucune socket : lancer via grocy-mcp.socket (systemd) ou indiquer --uds CHEMIN")
    settings = load_settings()
    app = build_app(
        settings,
        store=Store(settings.db_path),
        box=SecretBox(load_key(settings.key_path)),
        audit=AuditLog(settings.audit_path),
    )
    config = uvicorn.Config(
        app,
        fd=sock.fileno() if sock else None,
        uds=None if sock else args.uds,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )
    uvicorn.Server(config).run()
    return 0


def _generate_key(args: argparse.Namespace) -> int:
    from grocy_mcp.crypto import generate_key

    target = Path(args.file)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f"{target} existe déjà : refus d'écraser une clé") from None
    with os.fdopen(fd, "w") as f:
        f.write(generate_key() + "\n")
    os.chmod(target, 0o600)
    print(f"clé écrite dans {target} (0600)")
    return 0


def _rotate_key(args: argparse.Namespace) -> int:
    from grocy_mcp.crypto import SecretBox, load_key
    from grocy_mcp.store import Store

    db = os.environ.get("MCP_DB_PATH", "").strip()
    if not db:
        raise SystemExit("MCP_DB_PATH manquant")
    old, new = SecretBox(load_key(Path(args.old))), SecretBox(load_key(Path(args.new)))
    store = Store(db)
    try:
        count = store.rekey(lambda blob, aad: new.seal(old.open(blob, aad), aad))
    finally:
        store.close()
    print(f"{count} clé(s) Grocy re-chiffrée(s)")
    return 0


def _make_client(base_url: str, key: str):
    from grocy_mcp.grocy import GrocyClient

    return GrocyClient(base_url, key, timeout=30.0)


def _make_sources():
    from grocy_mcp.sources import FoodSources

    return FoodSources()


def _env_path(variable: str, directory: str, name: str) -> Path | None:
    """Chemin donné explicitement, sinon dans le dossier fourni par systemd (CREDENTIALS_DIRECTORY…)."""
    if os.environ.get(variable, "").strip():
        return Path(os.environ[variable].strip())
    if os.environ.get(directory, "").strip():
        return Path(os.environ[directory].strip()) / name
    return None


def _packaging(args: argparse.Namespace) -> int:
    """Passage de conditionnement (timer systemd) avec la clé du compte Grocy dédié auto-ia."""
    import asyncio

    from grocy_mcp.audit import AuditLog
    from grocy_mcp.config import ConfigError, bare_https
    from grocy_mcp.grocy import GrocyAuthError, GrocyForbidden, GrocyUnavailable
    from grocy_mcp.packaging_job import OffCache, run_packaging
    from grocy_mcp.redact import install_redaction

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    install_redaction()
    try:
        base_url = bare_https(os.environ, "GROCY_BASE_URL")
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    key_path = _env_path("MCP_AUTO_KEY_FILE", "CREDENTIALS_DIRECTORY", "auto-key")
    if key_path is None or not key_path.is_file():
        raise SystemExit("clé auto-ia introuvable : CREDENTIALS_DIRECTORY (auto-key) ou MCP_AUTO_KEY_FILE")
    key = key_path.read_text(encoding="utf-8").strip()
    cache = OffCache(_env_path("MCP_PACKAGING_STATE", "STATE_DIRECTORY", "off-cache.json"))
    audit_path = _env_path("MCP_PACKAGING_AUDIT", "LOGS_DIRECTORY", "conditionnement.jsonl")
    audit = AuditLog(audit_path) if audit_path and not args.dry_run else None
    try:
        report = asyncio.run(run_packaging(_make_client(base_url, key), _make_sources(), cache, dry_run=args.dry_run, audit=audit))
    except GrocyAuthError:
        print("clé auto-ia refusée par Grocy (révoquée ?)", file=sys.stderr)
        return 2
    except GrocyForbidden:
        print("le compte auto-ia n'a pas la permission MASTER_DATA_EDIT", file=sys.stderr)
        return 2
    except GrocyUnavailable:
        print("Grocy injoignable : nouvel essai au prochain passage", file=sys.stderr)
        return 1
    if args.dry_run:
        pending = sum(line["statut"] == "a_ecrire" for line in report["produits"])
        print(f"{pending} produit(s) à conditionner (simulation)")
    else:
        print(f"{report['ecrits']} produit(s) conditionné(s)")
    for line in report["produits"]:
        if line["statut"] != "deja":
            detail = f" — {line['contenance']} ({line['source']})" if line["contenance"] else ""
            print(f"  {line['statut']} : {line['produit']}{detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grocy_mcp", description="Serveur MCP pour Grocy")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="lancer le service")
    serve.add_argument("--uds", help="socket Unix locale (développement)")
    serve.set_defaults(handler=_serve)
    generate = commands.add_parser("generate-key", help="créer une clé maître (fichier 0600)")
    generate.add_argument("file")
    generate.set_defaults(handler=_generate_key)
    rotate = commands.add_parser("rotate-key", help="re-chiffrer les clés Grocy avec une nouvelle clé maître")
    rotate.add_argument("old")
    rotate.add_argument("new")
    rotate.set_defaults(handler=_rotate_key)
    packaging = commands.add_parser("conditionner", help="conditionnement automatique des produits (compte auto-ia)")
    packaging.add_argument("--dry-run", action="store_true", help="afficher sans rien écrire")
    packaging.set_defaults(handler=_packaging)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
