from pathlib import Path

import pytest

from grocy_mcp.config import ConfigError, load_settings


def env(**overrides):
    base = {
        "GROCY_BASE_URL": "https://grocy.example.test",
        "MCP_PUBLIC_URL": "https://mcp.example.test",
        "MCP_DB_PATH": "/tmp/mcp-test.db",
        "MCP_AUDIT_PATH": "/tmp/mcp-test-audit.jsonl",
        "MCP_KEY_FILE": "/tmp/mcp-test-key",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_minimal_configuration():
    s = load_settings(env())
    assert s.grocy_base_url == "https://grocy.example.test"
    assert s.resource_url == "https://mcp.example.test/mcp"
    assert s.host == "mcp.example.test"
    assert s.db_path == Path("/tmp/mcp-test.db")
    assert s.actions_per_minute == 30
    assert s.actions_per_hour == 300
    assert s.max_quantity == 50
    assert s.cimd_hosts == ("claude.ai", "chatgpt.com")


def test_trailing_slash_is_removed():
    assert load_settings(env(MCP_PUBLIC_URL="https://mcp.example.test/")).public_url == "https://mcp.example.test"


@pytest.mark.parametrize("name", ["GROCY_BASE_URL", "MCP_PUBLIC_URL"])
@pytest.mark.parametrize(
    "value",
    ["http://x.example.test", "https://x.example.test/chemin", "https://x.example.test?a=1", "ftp://x.example.test"],
)
def test_urls_must_be_bare_https(name, value):
    with pytest.raises(ConfigError):
        load_settings(env(**{name: value}))


@pytest.mark.parametrize("name", ["GROCY_BASE_URL", "MCP_PUBLIC_URL", "MCP_DB_PATH", "MCP_AUDIT_PATH"])
def test_missing_variable(name):
    with pytest.raises(ConfigError, match=name):
        load_settings(env(**{name: None}))


def test_key_from_systemd_credentials_directory():
    s = load_settings(env(MCP_KEY_FILE=None, CREDENTIALS_DIRECTORY="/run/credentials/grocy-mcp.service"))
    assert s.key_path == Path("/run/credentials/grocy-mcp.service/mcp-key")


def test_key_location_required():
    with pytest.raises(ConfigError, match="clé de chiffrement"):
        load_settings(env(MCP_KEY_FILE=None))


def test_integer_overrides():
    assert load_settings(env(MCP_ACTIONS_PER_MINUTE="5")).actions_per_minute == 5
    for bad in ("0", "-1", "abc"):
        with pytest.raises(ConfigError, match="MCP_ACTIONS_PER_MINUTE"):
            load_settings(env(MCP_ACTIONS_PER_MINUTE=bad))


def test_list_overrides():
    s = load_settings(env(MCP_EXTRA_REDIRECT_URIS="http://localhost:6274/oauth/callback  https://x.example.test/cb"))
    assert s.extra_redirect_uris == ("http://localhost:6274/oauth/callback", "https://x.example.test/cb")
    assert load_settings(env(MCP_CIMD_HOSTS="claude.ai")).cimd_hosts == ("claude.ai",)


def test_timezone():
    assert load_settings(env()).timezone == "UTC"
    assert load_settings(env(MCP_TIMEZONE="Europe/Zurich")).timezone == "Europe/Zurich"
    with pytest.raises(ConfigError, match="MCP_TIMEZONE"):
        load_settings(env(MCP_TIMEZONE="Mars/Olympus"))


def test_foyer_et_cle_usda():
    s = load_settings(env())
    assert s.household == () and s.usda_key_path is None
    s = load_settings(env(MCP_HOUSEHOLD="H:50,F:54,F:20", MCP_USDA_KEY_FILE="/tmp/usda-key"))
    assert s.household == (("H", 50), ("F", 54), ("F", 20)) and s.usda_key_path == Path("/tmp/usda-key")
    with pytest.raises(ConfigError, match="MCP_HOUSEHOLD"):
        load_settings(env(MCP_HOUSEHOLD="H:12"))
