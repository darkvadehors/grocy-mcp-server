import json
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


def _packaging_env(monkeypatch, tmp_path, fake):
    import httpx2

    import grocy_mcp.__main__ as cli
    from grocy_mcp.grocy import GrocyClient
    from grocy_mcp.sources import FoodSources

    key = tmp_path / "auto-key"
    from tests.fake_grocy import KEY_AUTO

    key.write_text(KEY_AUTO + "\n")
    monkeypatch.setenv("GROCY_BASE_URL", "https://grocy.example.test")
    monkeypatch.setenv("MCP_AUTO_KEY_FILE", str(key))
    monkeypatch.setenv("MCP_PACKAGING_STATE", str(tmp_path / "off.json"))
    monkeypatch.setenv("MCP_PACKAGING_AUDIT", str(tmp_path / "conditionnement.jsonl"))
    monkeypatch.setattr(cli, "_make_client", lambda base, k: GrocyClient(base, k, transport=fake.transport()))
    monkeypatch.setattr(cli, "_make_sources", lambda: FoodSources(transport=httpx2.MockTransport(lambda r: httpx2.Response(404))))


def test_conditionner(tmp_path, monkeypatch, capsys):
    from tests.fake_grocy import KEY_AUTO, FakeGrocy

    fake = FakeGrocy()
    fake.quantity_units.append({"id": 2, "name": "Gramme"})
    fake.products[5]["name"] = "Pâtes 500 g"
    _packaging_env(monkeypatch, tmp_path, fake)
    assert main(["conditionner", "--dry-run"]) == 0
    assert fake.conversions == [] and not (tmp_path / "conditionnement.jsonl").exists()
    assert main(["conditionner"]) == 0
    assert [(c["product_id"], c["from_qu_id"], c["to_qu_id"], c["factor"]) for c in fake.written_conversions()] == [(5, 1, 2, 500)]
    out = capsys.readouterr().out
    assert "1 produit(s) conditionné(s)" in out and "Pâtes 500 g" in out and KEY_AUTO not in out
    assert (tmp_path / "conditionnement.jsonl").exists()


def test_conditionner_refused_key(tmp_path, monkeypatch, capsys):
    from tests.fake_grocy import FakeGrocy

    fake = FakeGrocy()
    _packaging_env(monkeypatch, tmp_path, fake)
    (tmp_path / "auto-key").write_text("x" * 50)
    assert main(["conditionner"]) == 2
    assert "refusée" in capsys.readouterr().err


def test_conditionner_requires_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GROCY_BASE_URL", "https://grocy.example.test")
    monkeypatch.delenv("MCP_AUTO_KEY_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(SystemExit):
        main(["conditionner"])


def test_conditionner_enrichir(tmp_path, monkeypatch, capsys):
    """E7 : l'option --enrichir et la table des catégories (credential « categories » ou MCP_CATEGORY_MAP)."""
    import httpx2

    import grocy_mcp.__main__ as cli
    from grocy_mcp.sources import FoodSources
    from tests.fake_grocy import FakeGrocy

    fake = FakeGrocy()
    fake.product_groups = [{"id": 28, "name": "06.01 — Riz, pâtes, semoules et céréales"}]
    _packaging_env(monkeypatch, tmp_path, fake)
    product = {"status": 1, "product": {"categories_tags": ["en:pastas"]}}
    monkeypatch.setattr(cli, "_make_sources", lambda: FoodSources(transport=httpx2.MockTransport(
        lambda r: httpx2.Response(200, json=product))))
    rules = tmp_path / "categories"
    rules.write_text(json.dumps({"regles": [["en:pastas", "06.01"]]}))
    monkeypatch.delenv("MCP_CATEGORY_MAP", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert main(["conditionner", "--dry-run", "--enrichir"]) == 0
    assert "product_group_id" not in fake.products[1]
    assert "enrichi : Lait demi-écrémé — categorie" in capsys.readouterr().out
    assert main(["conditionner"]) == 0
    assert "product_group_id" not in fake.products[1]  # sans l'option : rien
    assert main(["conditionner", "--enrichir"]) == 0
    assert fake.products[1]["product_group_id"] == 28


@pytest.mark.parametrize("content", ["{", '{"regles": "x"}', '{"regles": [["en:a"]]}', '[["en:a", 5]]'])
def test_conditionner_table_illisible(tmp_path, monkeypatch, content):
    from tests.fake_grocy import FakeGrocy

    _packaging_env(monkeypatch, tmp_path, FakeGrocy())
    (tmp_path / "map.json").write_text(content)
    monkeypatch.setenv("MCP_CATEGORY_MAP", str(tmp_path / "map.json"))
    with pytest.raises(SystemExit, match="catégories"):
        main(["conditionner", "--enrichir"])
