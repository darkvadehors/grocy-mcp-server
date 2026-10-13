"""Point d'entrée : python -m grocy_mcp serve | generate-key FICHIER | rotate-key ANCIENNE NOUVELLE."""

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
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
