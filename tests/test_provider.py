import dataclasses
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import AnyUrl

from grocy_mcp.cimd import CimdError
from grocy_mcp.clients import ClientPolicy, LoopbackAwareClient
from grocy_mcp.provider import GrocyOAuthProvider
from grocy_mcp.store import sha256_hex
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull

pytestmark = pytest.mark.anyio
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK = "http://127.0.0.1:33418/callback"
RESOURCE = "https://mcp.example.test/mcp"
DAY = 86_400


class FakeCimd:
    def __init__(self):
        self.docs = {}
        self.calls = 0

    async def fetch(self, url):
        self.calls += 1
        if url not in self.docs:
            raise CimdError("absent")
        return self.docs[url]


@pytest.fixture
def cimd():
    return FakeCimd()


def make_provider(settings, store, audit, cimd, **overrides):
    return GrocyOAuthProvider(dataclasses.replace(settings, **overrides), store, ClientPolicy(()), cimd, audit)


@pytest.fixture
def provider(settings, store, audit, cimd):
    return make_provider(settings, store, audit, cimd)


def client_info(uri=CLAUDE, name="Claude", cid="c1"):
    return OAuthClientInformationFull(
        client_id=cid,
        client_secret="secret",
        redirect_uris=[AnyUrl(uri)],
        client_name=name,
        token_endpoint_auth_method="client_secret_post",
    )


def params(uri=CLAUDE, resource=RESOURCE, state="st"):
    return AuthorizationParams(
        state=state,
        scopes=[],
        code_challenge="challenge",
        redirect_uri=AnyUrl(uri),
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


async def connect(provider, uri=CLAUDE, user=3, cid="c1"):
    if await provider.get_client(cid) is None:
        await provider.register_client(client_info(uri, cid=cid))
    client = await provider.get_client(cid)
    url = provider.issue_code(client, params(uri), user)
    code = parse_qs(urlsplit(url).query)["code"][0]
    loaded = await provider.load_authorization_code(client, code)
    return client, await provider.exchange_authorization_code(client, loaded)


async def refresh(provider, client, tokens):
    loaded = await provider.load_refresh_token(client, tokens.refresh_token)
    assert loaded is not None
    return await provider.exchange_refresh_token(client, loaded, [])


async def test_register_and_get_client(provider, settings):
    await provider.register_client(client_info())
    client = await provider.get_client("c1")
    assert isinstance(client, LoopbackAwareClient)
    assert client.client_name == "Claude"
    assert '"event":"client_enregistre"' in settings.audit_path.read_text()
    assert await provider.get_client("inconnu") is None


async def test_register_applies_policy(provider):
    with pytest.raises(RegistrationError):
        await provider.register_client(client_info("https://evil.example/cb", name="Outil"))
    with pytest.raises(RegistrationError):
        await provider.register_client(client_info("https://chatgpt.com/connector_platform_oauth_redirect", name="Claude"))


async def test_register_quota_and_purge(settings, store, audit, cimd, clock):
    p = make_provider(settings, store, audit, cimd, max_clients=2)
    await p.register_client(client_info(cid="a"))
    await p.register_client(client_info(cid="b"))
    with pytest.raises(RegistrationError, match="trop de clients"):
        await p.register_client(client_info(cid="c"))
    clock.advance(DAY + 1)
    await p.register_client(client_info(cid="c"))  # a et b, jamais utilisés, sont purgés


async def test_authorize_creates_pending(provider, store):
    await provider.register_client(client_info())
    url = await provider.authorize(await provider.get_client("c1"), params())
    assert url.startswith("https://mcp.example.test/connexion?req=")
    req = parse_qs(urlsplit(url).query)["req"][0]
    assert len(req) >= 43
    assert store.get_pending(sha256_hex(req)).client_id == "c1"


async def test_authorize_resource_binding(provider):
    await provider.register_client(client_info())
    client = await provider.get_client("c1")
    with pytest.raises(AuthorizeError) as caught:
        await provider.authorize(client, params(resource="https://autre.example.test/mcp"))
    assert caught.value.error == "invalid_target"
    await provider.authorize(client, params(resource=None))
    await provider.authorize(client, params(resource=RESOURCE + "/"))


async def test_issue_code_redirect(provider):
    await provider.register_client(client_info())
    url = provider.issue_code(await provider.get_client("c1"), params(), 3)
    assert url.startswith(CLAUDE + "?")
    query = parse_qs(urlsplit(url).query)
    assert query["state"] == ["st"]
    assert query["iss"] == ["https://mcp.example.test"]
    assert len(query["code"][0]) >= 43


async def test_exchange_and_access_token(provider, store):
    client, tokens = await connect(provider)
    assert tokens.expires_in == 3600
    assert tokens.access_token.startswith("gma_") and tokens.refresh_token.startswith("gmr_")
    assert store.get_token(sha256_hex(tokens.access_token)) is not None  # stocké haché
    access = await provider.load_access_token(tokens.access_token)
    assert (access.subject, access.resource, access.client_id) == ("3", RESOURCE, "c1")
    assert access.claims["family"]


async def test_code_replay_revokes_connection(provider):
    await provider.register_client(client_info())
    client = await provider.get_client("c1")
    code = parse_qs(urlsplit(provider.issue_code(client, params(), 3)).query)["code"][0]
    loaded = await provider.load_authorization_code(client, code)
    tokens = await provider.exchange_authorization_code(client, loaded)
    assert await provider.load_authorization_code(client, code) is None
    assert await provider.load_access_token(tokens.access_token) is None
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, loaded)


async def test_code_expiry_and_client_binding(provider, clock):
    await provider.register_client(client_info())
    await provider.register_client(client_info(cid="c2"))
    client = await provider.get_client("c1")
    code = parse_qs(urlsplit(provider.issue_code(client, params(), 3)).query)["code"][0]
    assert await provider.load_authorization_code(await provider.get_client("c2"), code) is None
    clock.advance(61)
    assert await provider.load_authorization_code(client, code) is None


async def test_refresh_rotation_and_replay(provider):
    client, tokens = await connect(provider)
    renewed = await refresh(provider, client, tokens)
    assert renewed.refresh_token != tokens.refresh_token
    assert await provider.load_access_token(renewed.access_token) is not None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None  # rejeu
    assert await provider.load_access_token(renewed.access_token) is None  # toute la connexion est révoquée


async def test_access_token_lifetime(provider, clock):
    _, tokens = await connect(provider)
    clock.advance(3599)
    assert await provider.load_access_token(tokens.access_token) is not None
    clock.advance(2)
    assert await provider.load_access_token(tokens.access_token) is None


async def test_refresh_sliding_window(provider, clock):
    client, tokens = await connect(provider)
    clock.advance(29 * DAY)
    tokens = await refresh(provider, client, tokens)
    clock.advance(29 * DAY)
    tokens = await refresh(provider, client, tokens)
    clock.advance(31 * DAY)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_absolute_lifetime_90_days(provider, clock):
    client, tokens = await connect(provider)
    for _ in range(3):
        clock.advance(29 * DAY)
        tokens = await refresh(provider, client, tokens)
    clock.advance(4 * DAY)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
    assert await provider.load_access_token(tokens.access_token) is None


async def test_loopback_refresh_7_days(provider, clock):
    client, tokens = await connect(provider, uri=LOOPBACK, cid="code")
    clock.advance(6 * DAY)
    tokens = await refresh(provider, client, tokens)
    clock.advance(7 * DAY + 1)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_revoke_token(provider):
    _, tokens = await connect(provider)
    access = await provider.load_access_token(tokens.access_token)
    await provider.revoke_token(access)
    assert await provider.load_access_token(tokens.access_token) is None


async def test_max_connections_per_account(settings, store, audit, cimd):
    p = make_provider(settings, store, audit, cimd, max_connections_per_account=2)
    _, first = await connect(p)
    await connect(p)
    await connect(p)
    assert await p.load_access_token(first.access_token) is None


async def test_accounts_are_isolated(provider):
    _, johnny = await connect(provider, user=3)
    _, elli = await connect(provider, user=4)
    assert (await provider.load_access_token(johnny.access_token)).subject == "3"
    assert (await provider.load_access_token(elli.access_token)).subject == "4"


async def test_cimd_clients(provider, cimd, clock):
    url = "https://claude.ai/oauth/mcp-client-metadata.json"
    cimd.docs[url] = {"client_id": url, "client_name": "Claude", "redirect_uris": [CLAUDE]}
    client = await provider.get_client(url)
    assert client.token_endpoint_auth_method == "none"
    assert str(client.redirect_uris[0]) == CLAUDE
    await provider.get_client(url)
    assert cimd.calls == 1
    clock.advance(DAY + 1)
    await provider.get_client(url)
    assert cimd.calls == 2
    bad = "https://claude.ai/autre.json"
    cimd.docs[bad] = {"client_id": bad, "client_name": "Claude", "redirect_uris": ["https://evil.example/cb"]}
    assert await provider.get_client(bad) is None
    assert await provider.get_client("https://claude.ai/absent.json") is None
