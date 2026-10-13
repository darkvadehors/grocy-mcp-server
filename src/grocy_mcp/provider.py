"""Serveur d'autorisation OAuth 2.1 : interface OAuthAuthorizationServerProvider du SDK mcp 2.3.0 (choix A)."""

from __future__ import annotations

import json
import logging
import secrets

from pydantic import AnyUrl

from grocy_mcp.audit import AuditLog
from grocy_mcp.cimd import CimdError, CimdFetcher, CimdUnavailable
from grocy_mcp.clients import ClientPolicy, LoopbackAwareClient, clean_name, is_loopback
from grocy_mcp.config import Settings
from grocy_mcp.store import FamilyRow, Store, sha256_hex
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger(__name__)

CODE_TTL = 60
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86_400
LOOPBACK_REFRESH_TTL = 7 * 86_400
ABSOLUTE_TTL = 90 * 86_400
UNUSED_CLIENT_TTL = 3_600  # enregistrement jamais suivi d'une connexion : place libérée après 1 h
CIMD_CACHE_TTL = 86_400


class GrocyOAuthProvider:
    def __init__(
        self, settings: Settings, store: Store, policy: ClientPolicy, cimd: CimdFetcher, audit: AuditLog
    ) -> None:
        self.settings = settings
        self.store = store
        self.policy = policy
        self.cimd = cimd
        self.audit = audit

    # --- clients ---------------------------------------------------------------------------------
    async def get_client(self, client_id: str) -> LoopbackAwareClient | None:
        if client_id.startswith("https://"):
            return await self._cimd_client(client_id)
        row = self.store.get_client(client_id)
        return LoopbackAwareClient.model_validate_json(row[0]) if row else None

    async def _cimd_client(self, url: str) -> LoopbackAwareClient | None:
        row = self.store.get_client(url)
        if row and self.store.now() - row[1] < CIMD_CACHE_TTL:
            return LoopbackAwareClient.model_validate_json(row[0])
        try:
            document = await self.cimd.fetch(url)
            client = LoopbackAwareClient(
                client_id=url,
                client_name=document.get("client_name"),
                redirect_uris=document.get("redirect_uris"),
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
            self.policy.check(client)
        except CimdUnavailable:
            if row:  # document déjà validé : on le garde le temps que la panne passe, et on réessaie au prochain appel
                log.warning("métadonnées client injoignables, document validé précédent conservé")
                return LoopbackAwareClient.model_validate_json(row[0])
            log.warning("métadonnées client injoignables")
            return None
        except (CimdError, RegistrationError, ValueError) as error:
            log.warning("métadonnées client refusées (%s)", type(error).__name__)
            return None
        self.store.save_client(url, client.model_dump_json())
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.policy.check(client_info)
        self.store.purge_unused_clients(UNUSED_CLIENT_TTL)
        if self.store.count_clients() >= self.settings.max_clients:
            raise RegistrationError("invalid_client_metadata", "trop de clients enregistrés, réessayez plus tard")
        self.store.save_client(client_info.client_id, client_info.model_dump_json())
        self.audit.write("client_enregistre", client=client_info.client_id, nom=clean_name(client_info.client_name))

    # --- autorisation ----------------------------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self.settings.resource_url:
            raise AuthorizeError("invalid_target", "ressource inconnue de ce serveur")
        self.store.purge_expired()
        req = secrets.token_urlsafe(32)
        self.store.create_pending(sha256_hex(req), client.client_id, params.model_dump_json())
        return f"{self.settings.public_url}/connexion?req={req}"

    def issue_code(self, client: OAuthClientInformationFull, params: AuthorizationParams, grocy_user_id: int) -> str:
        now = self.store.now()
        family_id, revoked = self.store.create_family(
            grocy_user_id,
            client.client_id,
            clean_name(client.client_name),
            is_loopback(str(params.redirect_uri)),
            json.dumps(params.scopes or []),
            now + ABSOLUTE_TTL,
            self.settings.max_connections_per_account,
        )
        for old in revoked:
            self.audit.write("connexion_revoquee", connexion=old, motif="nombre maximal de connexions atteint")
        self.store.mark_client_used(client.client_id)
        code = secrets.token_urlsafe(32)
        data = {
            "redirect_uri": str(params.redirect_uri),
            "explicit": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
        }
        self.store.save_code(sha256_hex(code), family_id, client.client_id, json.dumps(data), now + CODE_TTL)
        query = {"code": code, "state": params.state, "iss": self.settings.public_url}
        return construct_redirect_uri(str(params.redirect_uri), **{k: v for k, v in query.items() if v is not None})

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.get_code(sha256_hex(authorization_code))
        if row is None or row.client_id != client.client_id:
            return None
        if row.used:
            self.revoke_family(row.family_id, "code d'autorisation rejoué")
            return None
        family = self._live_family(row.family_id)
        if family is None or self.store.now() >= row.expires_at:
            return None
        data = json.loads(row.data)
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=row.expires_at,
            client_id=row.client_id,
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["explicit"],
            resource=self.settings.resource_url,
            subject=str(family.grocy_user_id),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_hash = sha256_hex(authorization_code.code)
        row = self.store.get_code(code_hash)
        if row is None or not self.store.mark_code_used(code_hash):
            if row is not None:
                self.revoke_family(row.family_id, "code d'autorisation rejoué")
            raise TokenError("invalid_grant", "code d'autorisation déjà utilisé")
        return self._issue_tokens(row.family_id, client.client_id)

    # --- jetons ----------------------------------------------------------------------------------
    def _live_family(self, family_id: str) -> FamilyRow | None:
        family = self.store.get_family(family_id)
        if family is None or family.revoked_at is not None or self.store.now() >= family.absolute_expires_at:
            return None
        return family

    def _issue_tokens(self, family_id: str, client_id: str) -> OAuthToken:
        family = self._live_family(family_id)
        if family is None:
            raise TokenError("invalid_grant", "connexion révoquée ou expirée")
        now = self.store.now()
        access = "gma_" + secrets.token_urlsafe(32)
        refresh = "gmr_" + secrets.token_urlsafe(32)
        window = LOOPBACK_REFRESH_TTL if family.loopback else REFRESH_TTL
        access_expires = min(now + ACCESS_TTL, family.absolute_expires_at)
        refresh_expires = min(now + window, family.absolute_expires_at)
        self.store.save_token(sha256_hex(access), family_id, "access", client_id, access_expires)
        self.store.save_token(sha256_hex(refresh), family_id, "refresh", client_id, refresh_expires)
        scopes = json.loads(family.scopes)
        return OAuthToken(
            access_token=access,
            expires_in=int(access_expires - now),
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.get_token(sha256_hex(refresh_token))
        if row is None or row.kind != "refresh" or row.client_id != client.client_id:
            return None
        if row.used:
            self.revoke_family(row.family_id, "jeton de renouvellement rejoué")
            return None
        family = self._live_family(row.family_id)
        if family is None or self.store.now() >= row.expires_at:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=json.loads(family.scopes),
            expires_at=int(row.expires_at),
            resource=self.settings.resource_url,
            subject=str(family.grocy_user_id),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        token_hash = sha256_hex(refresh_token.token)
        row = self.store.get_token(token_hash)
        if row is None or not self.store.mark_token_used(token_hash):
            if row is not None:
                self.revoke_family(row.family_id, "jeton de renouvellement rejoué")
            raise TokenError("invalid_grant", "jeton de renouvellement déjà utilisé")
        return self._issue_tokens(row.family_id, client.client_id)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get_token(sha256_hex(token))
        if row is None or row.kind != "access" or self.store.now() >= row.expires_at:
            return None
        family = self._live_family(row.family_id)
        if family is None:
            return None
        self.store.touch_family(family.id)
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=json.loads(family.scopes),
            expires_at=int(row.expires_at),
            resource=self.settings.resource_url,
            subject=str(family.grocy_user_id),
            claims={"family": family.id},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = self.store.get_token(sha256_hex(token.token))
        if row is not None:
            self.revoke_family(row.family_id, "révocation demandée par le client")

    def revoke_family(self, family_id: str, reason: str) -> None:
        if self.store.revoke_family(family_id):
            self.audit.write("connexion_revoquee", connexion=family_id, motif=reason)
