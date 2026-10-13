"""Pages HTML : /connexion (consentement OAuth) et /connexions (gestion). Sans script ; CSP stricte ; jetons anti-CSRF."""

from __future__ import annotations

import html
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from grocy_mcp.audit import AuditLog
from grocy_mcp.clients import clean_name, display_host, origin
from grocy_mcp.config import Settings
from grocy_mcp.crypto import SecretBox
from grocy_mcp.grocy import GrocyClient, GrocyUser, InvalidKeyFormat, KeyCheck, check_key, normalize_key
from grocy_mcp.provider import GrocyOAuthProvider
from grocy_mcp.store import PendingRow, Store, sha256_hex
from mcp.server.auth.provider import AuthorizationParams, construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull

CSRF_COOKIE = "__Host-mcp_csrf"
SESSION_COOKIE = "__Host-mcp_gestion"
FORM_TTL = 600
# Lie la demande de connexion au navigateur qui a ouvert /authorize : un lien /connexion?req=… transmis à quelqu'un
# d'autre (hameçonnage du consentement) ne s'ouvre pas. SameSite=Lax car /authorize arrive depuis claude.ai ou chatgpt.com.
REQ_COOKIE = "__Host-mcp_req"
MAX_FORM_BYTES = 4096

KEY_MESSAGES = {
    KeyCheck.INVALID: (
        401,
        "Clé refusée. Copiez la clé API complète de votre compte dédié à l'IA "
        "(Grocy → menu utilisateur → Gérer les clés API).",
    ),
    KeyCheck.ADMIN: (
        403,
        "Cette clé appartient à un compte administrateur. Pour limiter les risques, "
        "utilisez la clé de votre compte dédié à l'IA (par exemple johnny-ia).",
    ),
    KeyCheck.UNAVAILABLE: (503, "Grocy ne répond pas pour le moment. Réessayez dans quelques instants."),
}

STYLE = """
:root { color-scheme: light dark; --fond: #f6f7f9; --carte: #fff; --texte: #1d2330; --doux: #5a6475;
  --accent: #2563eb; --erreur: #b42318; --bord: #d7dce4; }
@media (prefers-color-scheme: dark) { :root { --fond: #11151c; --carte: #1b212b; --texte: #e8ecf2;
  --doux: #9aa5b5; --accent: #6b9cff; --erreur: #ff8a80; --bord: #2c3442; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--fond); color: var(--texte);
  font: 17px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 34rem; margin: 2rem auto; padding: 1.5rem 1rem; background: var(--carte);
  border: 1px solid var(--bord); border-radius: 12px; }
h1 { font-size: 1.4rem; margin: 0 0 1rem; }
label { display: block; font-weight: 600; margin: 1rem 0 .4rem; }
input[type=password] { width: 100%; padding: .75rem; font-size: 1rem; border: 1px solid var(--bord);
  border-radius: 8px; background: transparent; color: inherit; }
.aide, small { color: var(--doux); font-size: .9rem; }
.erreur { color: var(--erreur); font-weight: 600; }
.actions { display: flex; gap: .75rem; flex-wrap: wrap; margin-top: 1.25rem; }
button { min-height: 44px; padding: .6rem 1.2rem; font-size: 1rem; border-radius: 8px; border: 0;
  background: var(--accent); color: #fff; cursor: pointer; }
button.secondaire { background: transparent; color: var(--texte); border: 1px solid var(--bord); }
ul { list-style: none; padding: 0; }
li { padding: .75rem 0; border-top: 1px solid var(--bord); }
li form { margin-top: .5rem; }
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def security_headers(form_action: str = "") -> dict[str, str]:
    actions = "'self'" + (f" {form_action}" if form_action else "")
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'self'; img-src 'self'; form-action {actions}; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def page(title: str, body: str, status: int = 200, form_action: str = "") -> HTMLResponse:
    document = (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{_e(title)}</title><link rel="stylesheet" href="/style.css"></head>'
        f"<body><main><h1>{_e(title)}</h1>{body}</main></body></html>"
    )
    return HTMLResponse(document, status_code=status, headers=security_headers(form_action))


def _set_cookie(response: Response, name: str, value: str) -> None:
    response.set_cookie(name, value, max_age=FORM_TTL, path="/", secure=True, httponly=True, samesite="strict")


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return "jamais"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%d/%m/%Y %H:%M UTC")


def bind_authorize(asgi_app, public_url: str):
    """Enveloppe la route /authorize du SDK : pose REQ_COOKIE quand elle redirige vers /connexion."""
    prefix = f"{public_url}/connexion?req=".encode()

    async def wrapped(scope, receive, send):
        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                location = next((v for k, v in headers if k.lower() == b"location"), b"")
                if location.startswith(prefix):
                    req = parse_qs(urlsplit(location.decode("latin-1")).query).get("req", [""])[0]
                    if req.replace("-", "").replace("_", "").isalnum():
                        cookie = f"{REQ_COOKIE}={req}; Max-Age={FORM_TTL}; Path=/; Secure; HttpOnly; SameSite=Lax"
                        headers.append((b"set-cookie", cookie.encode()))
                        message = {**message, "headers": headers}
            await send(message)

        await asgi_app(scope, receive, send_with_cookie)

    return wrapped


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


class Pages:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        box: SecretBox,
        provider: GrocyOAuthProvider,
        grocy_factory: Callable[[str], GrocyClient],
        audit: AuditLog,
    ) -> None:
        self.settings = settings
        self.store = store
        self.box = box
        self.provider = provider
        self.grocy_factory = grocy_factory
        self.audit = audit

    def routes(self) -> list[Route]:
        return [
            Route("/connexion", self.connexion, methods=["GET", "POST"]),
            Route("/connexions", self.connexions, methods=["GET", "POST"]),
            Route("/style.css", self.style, methods=["GET"]),
        ]

    async def style(self, request: Request) -> Response:
        return Response(
            STYLE, media_type="text/css", headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
        )

    # --- outils communs ------------------------------------------------------------------------------
    async def _form(self, request: Request) -> dict[str, str] | None:
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return None
        try:
            return dict(parse_qsl(body.decode(), keep_blank_values=True))
        except UnicodeDecodeError:
            return None

    def _pending(self, req: str) -> PendingRow | None:
        if not req or len(req) > 100:
            return None
        return self.store.get_pending(sha256_hex(req))

    @staticmethod
    def _csrf_matches(request: Request, form_csrf: str, stored_hash: str | None) -> bool:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        return bool(stored_hash and cookie and form_csrf) and (
            secrets.compare_digest(cookie, form_csrf) and secrets.compare_digest(sha256_hex(form_csrf), stored_hash)
        )

    async def _check_key(self, raw: str) -> tuple[KeyCheck, GrocyUser | None, str | None]:
        try:
            key = normalize_key(raw)
        except InvalidKeyFormat:
            return KeyCheck.INVALID, None, None
        outcome, user = await check_key(self.grocy_factory(key))
        return outcome, user, key if outcome is KeyCheck.OK else None

    @staticmethod
    def _same_browser(request: Request, req: str) -> bool:
        bound = request.cookies.get(REQ_COOKIE, "")
        return bool(bound and req) and secrets.compare_digest(bound, req)

    @staticmethod
    def _other_browser() -> HTMLResponse:
        return page(
            "Connexion non reconnue",
            "<p>Cette demande de connexion n'a pas été lancée depuis ce navigateur. Par sécurité, elle ne peut pas "
            "être poursuivie ici. Si vous voulez connecter Grocy, lancez la connexion vous-même depuis Claude ou "
            "ChatGPT ; si quelqu'un vous a envoyé ce lien, ignorez-le.</p>",
            400,
        )

    @staticmethod
    def _expired() -> HTMLResponse:
        return page(
            "Demande expirée",
            "<p>Cette demande de connexion a expiré ou a déjà été utilisée. "
            "Relancez la connexion depuis Claude ou ChatGPT.</p>",
            400,
        )

    @staticmethod
    def _session_lost(path: str) -> HTMLResponse:
        return page(
            "Session expirée",
            f'<p>La page a expiré. <a href="{_e(path)}">Recommencez</a> (ou relancez la connexion depuis Claude ou ChatGPT).</p>',
            400,
        )

    @staticmethod
    def _bad_request() -> HTMLResponse:
        return page("Requête invalide", "<p>Cette requête n'a pas pu être traitée.</p>", 400)

    # --- /connexion ----------------------------------------------------------------------------------
    def _key_page(
        self,
        req: str,
        csrf: str,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
        error: str = "",
        status: int = 200,
    ) -> HTMLResponse:
        redirect = str(params.redirect_uri)
        body = (
            f"<p><strong>{_e(clean_name(client.client_name))}</strong> demande l'accès à votre Grocy.</p>"
            f"<p>Après la connexion, vous serez renvoyé vers : <strong>{_e(display_host(redirect))}</strong></p>"
            + (f'<p class="erreur" role="alert">{_e(error)}</p>' if error else "")
            + '<form method="post" action="/connexion">'
            f'<input type="hidden" name="req" value="{_e(req)}"><input type="hidden" name="csrf" value="{_e(csrf)}">'
            "<p class=\"aide\">Ne continuez que si vous venez vous-même de lancer cette connexion depuis Claude ou "
            "ChatGPT. Si quelqu'un vous a envoyé ce lien, refusez.</p>"
            "<label for=\"cle\">Clé API Grocy de votre compte dédié à l'IA</label>"
            '<input id="cle" name="cle" type="password" autocomplete="off" autocapitalize="off" '
            'spellcheck="false" maxlength="200">'
            "<p class=\"aide\">Dans Grocy : menu utilisateur → Gérer les clés API. "
            "N'utilisez pas la clé d'un compte administrateur.</p>"
            '<div class="actions"><button type="submit" name="action" value="verifier">Vérifier la clé</button>'
            '<button type="submit" name="action" value="refuser" class="secondaire">Refuser</button></div></form>'
        )
        return page("Connexion à Grocy", body, status, form_action=origin(redirect))

    def _confirm_page(
        self, req: str, csrf: str, client: OAuthClientInformationFull, params: AuthorizationParams, username: str
    ) -> HTMLResponse:
        redirect = str(params.redirect_uri)
        body = (
            f"<p>Connecté en tant que <strong>{_e(username)}</strong>.</p>"
            f"<p>Autoriser <strong>{_e(clean_name(client.client_name))}</strong> ({_e(display_host(redirect))}) "
            "à consulter votre stock et votre liste de courses, et à enregistrer des achats, consommations "
            "et corrections en votre nom ?</p>"
            '<form method="post" action="/connexion">'
            f'<input type="hidden" name="req" value="{_e(req)}"><input type="hidden" name="csrf" value="{_e(csrf)}">'
            '<div class="actions"><button type="submit" name="action" value="autoriser">Autoriser</button>'
            '<button type="submit" name="action" value="refuser" class="secondaire">Refuser</button></div></form>'
        )
        return page("Confirmer l'accès", body, form_action=origin(redirect))

    async def connexion(self, request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            pending = self._pending(req)
            client = await self.provider.get_client(pending.client_id) if pending else None
            if pending is None or client is None:
                return self._expired()
            if not self._same_browser(request, req):
                return self._other_browser()
            csrf = secrets.token_urlsafe(32)
            self.store.set_pending_csrf(pending.id_hash, sha256_hex(csrf))
            response = self._key_page(req, csrf, client, AuthorizationParams.model_validate_json(pending.params))
            _set_cookie(response, CSRF_COOKIE, csrf)
            return response

        form = await self._form(request)
        if form is None:
            return self._bad_request()
        req, csrf, action = form.get("req", ""), form.get("csrf", ""), form.get("action", "")
        pending = self._pending(req)
        if pending is None:
            return self._expired()
        if not self._same_browser(request, req):
            return self._other_browser()
        if not self._csrf_matches(request, csrf, pending.csrf_hash):
            return self._session_lost(f"/connexion?req={req}")
        client = await self.provider.get_client(pending.client_id)
        if client is None:
            return self._expired()
        params = AuthorizationParams.model_validate_json(pending.params)

        if action == "refuser":
            self.store.delete_pending(pending.id_hash)
            self.audit.write("consentement_refuse", client=client.client_id)
            query = {"error": "access_denied", "state": params.state, "iss": self.settings.public_url}
            return _redirect(construct_redirect_uri(str(params.redirect_uri), **{k: v for k, v in query.items() if v}))

        if action == "verifier":
            outcome, user, key = await self._check_key(form.get("cle", ""))
            if outcome is not KeyCheck.OK or user is None or key is None:
                self.audit.write("cle_refusee", page="connexion", motif=outcome.value)
                status, message = KEY_MESSAGES[outcome]
                return self._key_page(req, csrf, client, params, error=message, status=status)
            new_csrf = secrets.token_urlsafe(32)
            sealed = self.box.seal(key, f"pending:{pending.id_hash}")
            self.store.set_pending_identity(pending.id_hash, sealed, user.id, user.username, sha256_hex(new_csrf))
            response = self._confirm_page(req, new_csrf, client, params, user.username)
            _set_cookie(response, CSRF_COOKIE, new_csrf)
            return response

        if action == "autoriser" and pending.enc_key is not None and pending.grocy_user_id is not None:
            key = self.box.open(pending.enc_key, f"pending:{pending.id_hash}")
            account_key = self.box.seal(key, f"grocy-user:{pending.grocy_user_id}")
            self.store.upsert_account(pending.grocy_user_id, pending.grocy_username or "", account_key)
            self.store.delete_pending(pending.id_hash)
            url = self.provider.issue_code(client, params, pending.grocy_user_id)
            self.audit.write(
                "connexion_autorisee",
                compte=pending.grocy_username,
                client=client.client_id,
                nom_client=clean_name(client.client_name),
            )
            return _redirect(url)
        return self._bad_request()

    # --- /connexions ---------------------------------------------------------------------------------
    def _login_page(self, csrf: str, error: str = "", status: int = 200) -> HTMLResponse:
        body = (
            "<p>Voir et révoquer les connexions de Claude, ChatGPT ou d'autres applications à votre Grocy.</p>"
            + (f'<p class="erreur" role="alert">{_e(error)}</p>' if error else "")
            + '<form method="post" action="/connexions">'
            f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
            "<label for=\"cle\">Clé API Grocy de votre compte dédié à l'IA</label>"
            '<input id="cle" name="cle" type="password" autocomplete="off" autocapitalize="off" '
            'spellcheck="false" maxlength="200">'
            '<div class="actions"><button type="submit" name="action" value="ouvrir">Afficher mes connexions</button></div>'
            "</form>"
        )
        return page("Mes connexions", body, status)

    def _list_page(self, user_id: int, csrf: str, notice: str = "") -> HTMLResponse:
        account = self.store.get_account(user_id)
        families = self.store.list_active_families(user_id)
        items = "".join(
            f"<li><strong>{_e(f.client_name or 'Application')}</strong>"
            + (" (application locale)" if f.loopback else "")
            + f"<br><small>Créée le {_when(f.created_at)} · dernier usage : {_when(f.last_used_at)}</small>"
            '<form method="post" action="/connexions">'
            f'<input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="connexion" value="{_e(f.id)}">'
            '<button type="submit" name="action" value="revoquer" class="secondaire">Révoquer</button></form></li>'
            for f in families
        )
        body = (
            f"<p>Compte : <strong>{_e(account.username if account else 'votre compte')}</strong></p>"
            + (f'<p role="status">{_e(notice)}</p>' if notice else "")
            + (f"<ul>{items}</ul>" if families else "<p>Aucune connexion active.</p>")
            + (
                '<form method="post" action="/connexions">'
                f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                '<div class="actions"><button type="submit" name="action" value="revoquer_tout" class="secondaire">'
                "Tout révoquer</button></div></form>"
                if families
                else ""
            )
        )
        return page("Mes connexions", body)

    async def connexions(self, request: Request) -> Response:
        if request.method == "GET":
            csrf = secrets.token_urlsafe(32)
            response = self._login_page(csrf)
            _set_cookie(response, CSRF_COOKIE, csrf)
            return response

        form = await self._form(request)
        if form is None:
            return self._bad_request()
        csrf, action = form.get("csrf", ""), form.get("action", "")

        if action == "ouvrir":
            if not self._csrf_matches(request, csrf, sha256_hex(request.cookies.get(CSRF_COOKIE, ""))):
                return self._session_lost("/connexions")
            outcome, user, _ = await self._check_key(form.get("cle", ""))
            if outcome is not KeyCheck.OK or user is None:
                self.audit.write("cle_refusee", page="connexions", motif=outcome.value)
                status, message = KEY_MESSAGES[outcome]
                return self._login_page(csrf, error=message, status=status)
            session, new_csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.store.create_session(sha256_hex(session), user.id, sha256_hex(new_csrf), self.store.now() + FORM_TTL)
            response = self._list_page(user.id, new_csrf)
            _set_cookie(response, SESSION_COOKIE, session)
            _set_cookie(response, CSRF_COOKIE, new_csrf)
            return response

        session = self.store.get_session(sha256_hex(request.cookies.get(SESSION_COOKIE, "")))
        if session is None or not self._csrf_matches(request, csrf, session[1]):
            return self._session_lost("/connexions")
        user_id = session[0]
        if action == "revoquer":
            family = self.store.get_family(form.get("connexion", ""))
            if family is None or family.grocy_user_id != user_id:
                return page("Connexion introuvable", "<p>Cette connexion n'existe pas.</p>", 404)
            self.provider.revoke_family(family.id, "révoquée depuis la page /connexions")
            notice = "Connexion révoquée."
        elif action == "revoquer_tout":
            for family in self.store.list_active_families(user_id):
                self.provider.revoke_family(family.id, "révoquée depuis la page /connexions")
            notice = "Toutes les connexions ont été révoquées."
        else:
            return self._bad_request()
        return self._list_page(user_id, csrf, notice)
