import os
import stat

import pytest

from grocy_mcp.__main__ import main, systemd_socket
from grocy_mcp.crypto import SecretBox, load_key
from grocy_mcp.store import Store


def test_generate_key(tmp_path, capsys):
    target = tmp_path / "mcp-key"
    assert main(["generate-key", str(target)]) == 0
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert len(load_key(target)) == 32
    assert target.read_text().strip() not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["generate-key", str(target)])


def test_rotate_key(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    main(["generate-key", str(old)])
    main(["generate-key", str(new)])
    db = tmp_path / "mcp.db"
    store = Store(db)
    store.upsert_account(3, "johnny-ia", SecretBox(load_key(old)).seal("cle-grocy", "grocy-user:3"))
    store.close()
    monkeypatch.setenv("MCP_DB_PATH", str(db))
    assert main(["rotate-key", str(old), str(new)]) == 0
    store = Store(db)
    assert SecretBox(load_key(new)).open(store.get_account(3).enc_key, "grocy-user:3") == "cle-grocy"
    store.close()


def test_rotate_key_requires_db(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_DB_PATH", raising=False)
    with pytest.raises(SystemExit):
        main(["rotate-key", str(tmp_path / "a"), str(tmp_path / "b")])


def test_systemd_socket_absent(monkeypatch):
    monkeypatch.delenv("LISTEN_PID", raising=False)
    assert systemd_socket() is None
    monkeypatch.setenv("LISTEN_PID", "1")
    assert systemd_socket() is None


def test_serve_requires_a_socket(monkeypatch, tmp_path, settings):
    for name, value in {
        "GROCY_BASE_URL": settings.grocy_base_url,
        "MCP_PUBLIC_URL": settings.public_url,
        "MCP_DB_PATH": str(settings.db_path),
        "MCP_AUDIT_PATH": str(settings.audit_path),
        "MCP_KEY_FILE": str(settings.key_path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LISTEN_PID", raising=False)
    with pytest.raises(SystemExit, match="socket"):
        main(["serve"])
