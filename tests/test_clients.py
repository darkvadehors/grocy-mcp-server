import pytest
from pydantic import AnyUrl

from grocy_mcp.clients import (
    ClientPolicy,
    LoopbackAwareClient,
    claimed_provider,
    classify_redirect,
    clean_name,
    display_host,
    origin,
)
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull

CLAUDE = "https://claude.ai/api/mcp/auth_callback"
CHATGPT = "https://chatgpt.com/connector_platform_oauth_redirect"


def info(*uris, name=None):
    return OAuthClientInformationFull(client_id="c", redirect_uris=[AnyUrl(u) for u in uris], client_name=name)


@pytest.mark.parametrize(
    ("uri", "kind"),
    [
        (CLAUDE, "claude"),
        ("https://claude.com/api/mcp/auth_callback", "claude"),
        (CHATGPT, "chatgpt"),
        ("https://chatgpt.com/connector/oauth/AbC_12-x", "chatgpt"),
        ("http://localhost:33418/callback", "loopback"),
        ("http://127.0.0.1/callback", "loopback"),
        ("http://[::1]:5000/cb", "loopback"),
        ("https://claude.ai/api/mcp/auth_callback/x", None),
        ("https://chatgpt.com/connector/oauth/../../x", None),
        ("https://evil.example/callback", None),
        ("http://localhost.evil.example/callback", None),
        ("http://localhost:3000/cb?x=1", None),
        ("https://localhost/callback", None),
        ("javascript:alert(1)", None),
    ],
)
def test_classify_redirect(uri, kind):
    assert classify_redirect(uri) == kind


def test_extra_redirects():
    assert classify_redirect("https://inspector.example.test/cb", ("https://inspector.example.test/cb",)) == "extra"


@pytest.mark.parametrize(
    ("name", "provider"),
    [("Claude", "claude"), ("claude code", "claude"), ("Anthropic MCP", "claude"), ("ChatGPT", "chatgpt"),
     ("OpenAI connector", "chatgpt"), ("Mon outil", None), (None, None), ("C l a u d e", None), ("Сlaude", "claude")],
)
def test_claimed_provider(name, provider):
    assert claimed_provider(name) == provider


def test_policy_accepts_official_clients():
    policy = ClientPolicy(())
    policy.check(info(CLAUDE, name="Claude"))
    policy.check(info(CHATGPT, name="ChatGPT"))
    policy.check(info("http://localhost:4000/callback", name="Claude Code"))
    policy.check(info("http://localhost:4000/callback", name="Mon outil"))


@pytest.mark.parametrize(
    ("uris", "name", "error"),
    [
        ((), "Claude", "invalid_redirect_uri"),
        (("https://evil.example/cb",), "Outil", "invalid_redirect_uri"),
        ((CHATGPT,), "Claude", "invalid_client_metadata"),
        (("https://extra.example.test/cb",), "Claude", "invalid_client_metadata"),
    ],
)
def test_policy_refusals(uris, name, error):
    policy = ClientPolicy(("https://extra.example.test/cb",))
    with pytest.raises(RegistrationError) as caught:
        policy.check(OAuthClientInformationFull(client_id="c", redirect_uris=[AnyUrl(u) for u in uris] or None, client_name=name))
    assert caught.value.error == error


def test_loopback_port_is_free_but_path_and_host_are_not():
    client = LoopbackAwareClient(client_id="c", redirect_uris=[AnyUrl("http://127.0.0.1:33418/callback"), AnyUrl(CLAUDE)])
    assert str(client.validate_redirect_uri(AnyUrl("http://127.0.0.1:51000/callback"))) == "http://127.0.0.1:51000/callback"
    assert str(client.validate_redirect_uri(AnyUrl(CLAUDE))) == CLAUDE
    for bad in ("http://127.0.0.1:51000/autre", "http://localhost:51000/callback", "http://127.0.0.1:51000/callback?x=1"):
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(bad))


def test_display_and_origin():
    assert display_host(CLAUDE) == "claude.ai"
    assert display_host("http://127.0.0.1:5000/callback") == "une application locale sur cet appareil"
    assert origin("http://127.0.0.1:5000/callback") == "http://127.0.0.1:5000"
    assert origin(CLAUDE) == "https://claude.ai"


def test_clean_name():
    assert clean_name(None) == "Application sans nom"
    assert clean_name("A\x00B‮C") == "ABC"
    assert len(clean_name("x" * 500)) == 60
