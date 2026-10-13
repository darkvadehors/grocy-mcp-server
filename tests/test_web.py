import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from pydantic import AnyUrl
from starlette.applications import Starlette

from grocy_mcp.cimd import CimdFetcher
from grocy_mcp.clients import ClientPolicy
from grocy_mcp.grocy import GrocyClient
from grocy_mcp.provider import GrocyOAuthProvider
from grocy_mcp.web import Pages
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from tests.fake_grocy import KEY_ADMIN, KEY_ELLI_IA, KEY_JOHNNY_IA

pytestmark = pytest.mark.anyio
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_of(response):
    return CSRF.search(response.text).group(1)


@pytest.fixture
async def env(settings, store, box, audit, fake):
    provider = GrocyOAuthProvider(settings, store, ClientPolicy(()), CimdFetcher(()), audit)
    factory = lambda key: GrocyClient(settings.grocy_base_url, key, transport=fake.transport())  # noqa: E731
    app = Starlette(routes=Pages(settings, store, box, provider, factory, audit).routes())
    for cid, name, uri in [("c1", "Claude", CLAUDE), ("c2", "<img src=x onerror=alert(1)>", "http://127.0.0.1:4000/callback")]:
        await provider.register_client(
            OAuthClientInformationFull(
                client_id=cid,
                client_secret="s",
                redirect_uris=[AnyUrl(uri)],
                client_name=name,
                token_endpoint_auth_method="client_secret_post",
            )
        )
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://mcp.example.test") as http:
        yield SimpleNamespace(http=http, provider=provider, store=store, box=box, settings=settings, fake=fake)


async def start(env, cid="c1", uri=CLAUDE, state="st"):
    params = AuthorizationParams(
        state=state, scopes=[], code_challenge="ch", redirect_uri=AnyUrl(uri),
        redirect_uri_provided_explicitly=True, resource=None,
    )
    url = await env.provider.authorize(await env.provider.get_client(cid), params)
    req = parse_qs(urlsplit(url).query)["req"][0]
    return req, await env.http.get(f"/connexion?req={req}")


async def post(env, path, **fields):
    return await env.http.post(path, data=fields)


async def test_key_page(env):
    _, page = await start(env)
    assert page.status_code == 200
    assert "<strong>Claude</strong> demande l'accès" in page.text.replace("&#x27;", "'")
    assert "<strong>claude.ai</strong>" in page.text
    assert 'type="password"' in page.text and 'autocomplete="off"' in page.text
    assert "<script" not in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self' https://claude.ai" in csp and "script-src" not in csp
    assert page.headers["cache-control"] == "no-store"
    cookie = page.headers["set-cookie"]
    assert cookie.startswith("__Host-mcp_csrf=") and "Secure" in cookie and "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower() and "Path=/" in cookie


async def test_full_consent(env):
    req, page = await start(env)
    confirm = await post(env, "/connexion", req=req, csrf=csrf_of(page), action="verifier", cle=f"  {KEY_JOHNNY_IA}\n")
    assert confirm.status_code == 200
    assert "Connecté en tant que <strong>johnny-ia</strong>" in confirm.text
    assert KEY_JOHNNY_IA not in confirm.text
    done = await post(env, "/connexion", req=req, csrf=csrf_of(confirm), action="autoriser")
    assert done.status_code == 303
    query = parse_qs(urlsplit(done.headers["location"]).query)
    assert done.headers["location"].startswith(CLAUDE + "?")
    assert query["state"] == ["st"] and query["iss"] == ["https://mcp.example.test"] and query["code"]
    account = env.store.get_account(3)
    assert env.box.open(account.enc_key, "grocy-user:3") == KEY_JOHNNY_IA
    again = await post(env, "/connexion", req=req, csrf=csrf_of(confirm), action="autoriser")
    assert again.status_code == 400 and "expiré" in again.text


@pytest.mark.parametrize(
    ("key", "status", "message"),
    [("x" * 50, 401, "Clé refusée"), ("pas une clé !", 401, "Clé refusée"), (KEY_ADMIN, 403, "administrateur")],
)
async def test_refused_keys(env, key, status, message):
    req, page = await start(env)
    response = await post(env, "/connexion", req=req, csrf=csrf_of(page), action="verifier", cle=key)
    assert response.status_code == status
    assert message in response.text
    assert key not in env.settings.audit_path.read_text()
    assert '"event":"cle_refusee"' in env.settings.audit_path.read_text()
    assert env.store.get_account(1) is None


async def test_grocy_unavailable(env):
    req, page = await start(env)
    env.fake.next_failure = 503
    response = await post(env, "/connexion", req=req, csrf=csrf_of(page), action="verifier", cle=KEY_JOHNNY_IA)
    assert response.status_code == 503 and "ne répond pas" in response.text


async def test_csrf_is_required(env):
    req, page = await start(env)
    token = csrf_of(page)
    assert (await post(env, "/connexion", req=req, csrf="faux", action="verifier", cle=KEY_JOHNNY_IA)).status_code == 400
    env.http.cookies.clear()
    assert (await post(env, "/connexion", req=req, csrf=token, action="verifier", cle=KEY_JOHNNY_IA)).status_code == 400


async def test_refuse(env):
    req, page = await start(env)
    response = await post(env, "/connexion", req=req, csrf=csrf_of(page), action="refuser")
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == ["access_denied"] and query["state"] == ["st"] and query["iss"]


async def test_expired_or_unknown_request(env, clock):
    assert (await env.http.get("/connexion?req=inconnu")).status_code == 400
    assert (await env.http.get("/connexion")).status_code == 400
    req, _ = await start(env)
    clock.advance(601)
    response = await env.http.get(f"/connexion?req={req}")
    assert response.status_code == 400 and "expiré" in response.text


async def test_names_are_escaped_and_loopback_is_explained(env):
    _, page = await start(env, cid="c2", uri="http://127.0.0.1:4000/callback")
    assert "<img" not in page.text and "&lt;img" in page.text
    assert "une application locale sur cet appareil" in page.text
    assert "form-action 'self' http://127.0.0.1:4000" in page.headers["content-security-policy"]


async def test_oversized_form(env):
    req, page = await start(env)
    response = await post(env, "/connexion", req=req, csrf=csrf_of(page), action="verifier", cle="a" * 5000)
    assert response.status_code == 400


async def test_connexions_page(env, clock):
    now = clock()
    mine, _ = env.store.create_family(3, "c1", "Claude", False, "[]", now + 1000, 10)
    other, _ = env.store.create_family(4, "c1", "Claude", False, "[]", now + 1000, 10)
    login = await env.http.get("/connexions")
    assert login.status_code == 200 and 'type="password"' in login.text
    refused = await post(env, "/connexions", csrf=csrf_of(login), action="ouvrir", cle=KEY_ADMIN)
    assert refused.status_code == 403
    listing = await post(env, "/connexions", csrf=csrf_of(login), action="ouvrir", cle=KEY_JOHNNY_IA)
    assert listing.status_code == 200
    assert mine in listing.text and other not in listing.text
    token = csrf_of(listing)
    forbidden = await post(env, "/connexions", csrf=token, action="revoquer", connexion=other)
    assert forbidden.status_code == 404
    assert env.store.get_family(other).revoked_at is None
    done = await post(env, "/connexions", csrf=token, action="revoquer", connexion=mine)
    assert done.status_code == 200 and env.store.get_family(mine).revoked_at is not None
    assert (await post(env, "/connexions", csrf="faux", action="revoquer_tout")).status_code == 400


async def test_connexions_session_expires(env, clock):
    login = await env.http.get("/connexions")
    listing = await post(env, "/connexions", csrf=csrf_of(login), action="ouvrir", cle=KEY_ELLI_IA)
    assert listing.status_code == 200
    token = env.http.cookies.get("__Host-mcp_csrf")  # aucune connexion active : la page n'affiche pas de formulaire
    assert token and (await post(env, "/connexions", csrf=token, action="revoquer_tout")).status_code == 200
    clock.advance(601)
    assert (await post(env, "/connexions", csrf=token, action="revoquer_tout")).status_code == 400


async def test_stylesheet(env):
    response = await env.http.get("/style.css")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/css")
