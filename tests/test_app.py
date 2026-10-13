import base64
import hashlib
import json
import logging
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from grocy_mcp.app import build_app
from grocy_mcp.cimd import CimdFetcher
from tests.fake_grocy import KEY_ADMIN, KEY_ELLI_IA, KEY_JOHNNY_IA

pytestmark = pytest.mark.anyio
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://mcp.example.test/mcp"
CSRF = re.compile(r'name="csrf" value="([^"]+)"')
TOOLS = {
    "rechercher_produit", "voir_stock", "voir_peremptions", "voir_produit", "voir_liste_courses",
    "recettes_faisables", "voir_references", "ajouter_liste_courses", "retirer_liste_courses",
    "ajouter_manquants_liste", "enregistrer_achat", "consommer", "ouvrir", "deplacer",
    "corriger_inventaire", "annuler_derniere_action", "importer_articles", "creer_emplacement", "creer_categorie"}
SECRETS: list[str] = []


@pytest.fixture
async def http(settings, store, box, audit, fake):
    SECRETS.clear()
    app = build_app(settings, store=store, box=box, audit=audit, grocy_transport=fake.transport(), cimd_fetcher=CimdFetcher(()))
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=settings.public_url) as client:
            yield client


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    SECRETS.append(verifier)
    return verifier, challenge


async def register(http, redirect=CLAUDE, name="Claude"):
    return await http.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": name,
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


async def authorize(http, client, challenge, redirect=CLAUDE, resource=RESOURCE):
    return await http.get(
        "/authorize",
        params={
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "etat", "resource": resource,
        },
    )


async def consent(http, location, key):
    page = await http.get(location)
    req = parse_qs(urlsplit(location).query)["req"][0]
    confirm = await http.post("/connexion", data={"req": req, "csrf": CSRF.search(page.text).group(1), "action": "verifier", "cle": key})
    if confirm.status_code != 200:
        return confirm
    return await http.post("/connexion", data={"req": req, "csrf": CSRF.search(confirm.text).group(1), "action": "autoriser"})


async def exchange(http, client, code, verifier, redirect=CLAUDE):
    return await http.post(
        "/token",
        data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "code_verifier": verifier,
            "client_id": client["client_id"], "client_secret": client["client_secret"], "resource": RESOURCE,
        },
    )


async def login(http, key=KEY_JOHNNY_IA):
    client = (await register(http)).json()
    SECRETS.append(client["client_secret"])
    verifier, challenge = pkce()
    done = await consent(http, (await authorize(http, client, challenge)).headers["location"], key)
    query = parse_qs(urlsplit(done.headers["location"]).query)
    assert query["iss"] == ["https://mcp.example.test"] and query["state"] == ["etat"]
    code = query["code"][0]
    tokens = (await exchange(http, client, code, verifier)).json()
    SECRETS.extend([code, tokens["access_token"], tokens["refresh_token"]])
    return client, tokens, code, verifier


async def rpc(http, access, method, params=None, origin=None):
    headers = {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if origin:
        headers["Origin"] = origin
    init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    first = await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
    if first.status_code != 200:
        return first
    headers["MCP-Protocol-Version"] = first.json()["result"]["protocolVersion"]
    return await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}})


async def tool(http, access, name, **arguments):
    response = await rpc(http, access, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = result["content"][0]["text"]
    return (True, text) if result.get("isError") else (False, json.loads(text))


async def test_o1_discovery(http):
    response = await http.post("/mcp", json={})
    assert response.status_code == 401
    assert 'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"' in response.headers["www-authenticate"]
    resource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["resource"] == RESOURCE and resource["authorization_servers"] == ["https://mcp.example.test"]
    metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == "https://mcp.example.test"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["registration_endpoint"] == "https://mcp.example.test/register"
    assert metadata["revocation_endpoint"] == "https://mcp.example.test/revoke"
    assert (await http.get("/sante")).text == "ok"


async def test_o2_full_flow(http, fake):
    client, tokens, _, _ = await login(http)
    assert tokens["expires_in"] == 3600 and tokens["token_type"] == "Bearer"
    listing = (await rpc(http, tokens["access_token"], "tools/list")).json()["result"]["tools"]
    assert {t["name"] for t in listing} == TOOLS
    hints = {t["name"]: t["annotations"] for t in listing}
    assert hints["voir_stock"]["readOnlyHint"] is True and hints["consommer"]["destructiveHint"] is True
    error, stock = await tool(http, tokens["access_token"], "voir_stock")
    assert not error and len(stock["produits"]) == 4
    renewed = await http.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
              "client_id": client["client_id"], "client_secret": client["client_secret"]},
    )
    assert renewed.status_code == 200
    SECRETS.extend([renewed.json()["access_token"], renewed.json()["refresh_token"]])
    error, bought = await tool(http, renewed.json()["access_token"], "enregistrer_achat", produit="lait", quantite=2)
    assert not error and bought["statut"] == "ok" and fake.amount(1) == 4
    error, undone = await tool(http, renewed.json()["access_token"], "annuler_derniere_action")
    assert not error and undone["statut"] == "ok" and fake.amount(1) == 2


async def test_o3_wrong_pkce_and_code_replay(http):
    client = (await register(http)).json()
    verifier, challenge = pkce()
    done = await consent(http, (await authorize(http, client, challenge)).headers["location"], KEY_JOHNNY_IA)
    code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
    wrong = await exchange(http, client, code, "x" * 64)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    first = await exchange(http, client, code, verifier)
    assert first.status_code == 200
    replay = await exchange(http, client, code, verifier)
    assert replay.status_code == 400
    assert (await http.post("/mcp", headers={"Authorization": f"Bearer {first.json()['access_token']}"}, json={})).status_code == 401


async def test_o3_refresh_replay_revokes(http):
    client, tokens, _, _ = await login(http)
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"], "client_secret": client["client_secret"]}
    renewed = await http.post("/token", data=data)
    assert renewed.status_code == 200
    assert (await http.post("/token", data=data)).status_code == 400
    assert (await rpc(http, renewed.json()["access_token"], "tools/list")).status_code == 401


async def test_o3_registration_and_authorization_refusals(http):
    assert (await register(http, "https://evil.example/cb", "Outil")).status_code == 400
    assert (await register(http, "https://chatgpt.com/connector_platform_oauth_redirect", "Claude")).status_code == 400
    client = (await register(http)).json()
    _, challenge = pkce()
    bad_redirect = await authorize(http, client, challenge, redirect="https://claude.ai/autre")
    assert bad_redirect.status_code == 400 and "location" not in bad_redirect.headers
    other = await authorize(http, client, challenge, resource="https://autre.example.test/mcp")
    assert other.status_code == 302 and "error=invalid_target" in other.headers["location"]


async def test_o3_admin_and_invalid_keys(http):
    client = (await register(http)).json()
    _, challenge = pkce()
    admin = await consent(http, (await authorize(http, client, challenge)).headers["location"], KEY_ADMIN)
    assert admin.status_code == 403
    invalid = await consent(http, (await authorize(http, client, challenge)).headers["location"], "z" * 50)
    assert invalid.status_code == 401


async def test_s1_origin_check(http):
    _, tokens, _, _ = await login(http)
    assert (await rpc(http, tokens["access_token"], "tools/list", origin="https://evil.example")).status_code == 403
    assert (await rpc(http, tokens["access_token"], "tools/list", origin="https://claude.ai")).status_code == 200


async def test_tool_errors_are_readable(http):
    _, tokens, _, _ = await login(http)
    error, text = await tool(http, tokens["access_token"], "consommer", produit="lait", quantite=500)
    assert error and "limitée à 50" in text


async def test_isolation_between_accounts(http, fake):
    _, johnny, _, _ = await login(http, KEY_JOHNNY_IA)
    _, elli, _, _ = await login(http, KEY_ELLI_IA)
    await tool(http, johnny["access_token"], "enregistrer_achat", produit="lait", quantite=1)
    await tool(http, elli["access_token"], "enregistrer_achat", produit="beurre", quantite=1)
    assert {(r["product_id"], r["user_id"]) for r in fake.log} == {(1, 3), (4, 4)}
    _, elli_undo = await tool(http, elli["access_token"], "annuler_derniere_action")
    assert elli_undo["statut"] == "ok" and fake.amount(1) == 3 and fake.amount(4) == 1


async def test_s1_no_secret_in_logs(http, caplog, settings):
    caplog.set_level(logging.DEBUG)
    client, tokens, code, verifier = await login(http)
    await tool(http, tokens["access_token"], "enregistrer_achat", produit="lait", quantite=1)
    await tool(http, tokens["access_token"], "consommer", produit="lait", quantite=999)
    await exchange(http, client, code, verifier)  # rejeu : révoque la connexion
    await test_o3_admin_and_invalid_keys(http)
    secrets_seen = SECRETS + [KEY_JOHNNY_IA, KEY_ADMIN]
    journals = caplog.text + settings.audit_path.read_text()
    for secret in secrets_seen:
        assert secret not in journals



async def test_authorize_binds_request_to_browser(http):
    client = (await register(http)).json()
    SECRETS.append(client["client_secret"])
    _, challenge = pkce()
    response = await authorize(http, client, challenge)
    cookie = response.headers.get("set-cookie", "").lower()
    assert cookie.startswith("__host-mcp_req=") and "samesite=lax" in cookie and "secure" in cookie and "httponly" in cookie


async def test_import_de_bout_en_bout(http, fake):
    """importer_articles via MCP : aperçu, confirmation, création par la recherche multi-sources ; 19 outils exposés."""
    _, tokens, _, _ = await login(http)
    access = tokens["access_token"]
    listing = (await rpc(http, access, "tools/list")).json()["result"]["tools"]
    assert len(listing) == 19
    importer = next(t for t in listing if t["name"] == "importer_articles")
    assert "FoodShiner" in importer["description"] and importer["annotations"]["destructiveHint"] is False
    fake.lookup_db["5449000000996"] = {"name": "Coca-Cola 33 cl"}
    articles = [{"code_barres": "5449000000996", "quantite": 1, "emplacement": "Frigo"}]
    error, preview = await tool(http, access, "importer_articles", articles=articles)
    assert not error and preview["statut"] == "confirmation_requise"
    error, done = await tool(http, access, "importer_articles", articles=articles, jeton_confirmation=preview["jeton_confirmation"])
    assert not error and done["statut"] == "ok" and "5449000000996" in fake.barcodes
    error, created = await tool(http, access, "creer_categorie", nom="Boissons")
    assert not error and created["cree"] is True
