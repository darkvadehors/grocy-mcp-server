"""Assemblage : MCPServer du SDK avec serveur d'autorisation intégré (choix A), 16 outils, pages, métadonnées OAuth."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from grocy_mcp import __version__
from grocy_mcp.audit import AuditLog
from grocy_mcp.cimd import CimdFetcher
from grocy_mcp.clients import ClientPolicy
from grocy_mcp.config import Settings
from grocy_mcp.crypto import SecretBox
from grocy_mcp.grocy import GrocyClient
from grocy_mcp.provider import GrocyOAuthProvider
from grocy_mcp.store import Store
from grocy_mcp.tools import Caller, ToolFailure, ToolService
from grocy_mcp.web import Pages
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations

INSTRUCTIONS = (
    "Accès au Grocy de la personne connectée : stock, dates de péremption, liste de courses et recettes. "
    "Les outils d'action modifient réellement son stock. Si un nom de produit est ambigu, aucune action n'est faite : "
    "demandez à la personne de choisir parmi les candidats renvoyés. Les opérations importantes renvoient d'abord "
    "un aperçu et un jeton_confirmation : montrez l'aperçu et ne confirmez qu'avec l'accord de la personne. "
    "En cas d'erreur, annuler_derniere_action annule la dernière action de stock faite via ce connecteur (24 h)."
)
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


def current_caller() -> Caller:
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("Authentification requise.")
    return Caller(int(token.subject), str((token.claims or {}).get("family", "")), token.client_id)


async def _call(method: Callable[..., Awaitable[dict]], *args: object, **kwargs: object) -> dict:
    try:
        return await method(current_caller(), *args, **kwargs)
    except ToolFailure as failure:
        raise ToolError(str(failure)) from None


def register_tools(server: MCPServer, service: ToolService) -> None:
    @server.tool(title="Rechercher un produit", annotations=READ)
    async def rechercher_produit(texte: str) -> dict:
        """Cherche des produits par nom (recherche partielle, accents ignorés) ou par code-barres (8 à 14 chiffres)."""
        return await _call(service.rechercher_produit, texte)

    @server.tool(title="Voir le stock", annotations=READ)
    async def voir_stock(emplacement: str | None = None) -> dict:
        """Liste les produits en stock (quantité, ouverts, prochaine péremption).
        emplacement : filtre facultatif sur l'emplacement par défaut du produit (ex. « Frigo »)."""
        return await _call(service.voir_stock, emplacement)

    @server.tool(title="Voir les péremptions", annotations=READ)
    async def voir_peremptions(jours: int = 5) -> dict:
        """Produits qui périment dans les `jours` prochains jours (1 à 60), produits dépassés ou périmés,
        et produits sous leur stock minimum."""
        return await _call(service.voir_peremptions, jours)

    @server.tool(title="Voir un produit", annotations=READ)
    async def voir_produit(produit: str) -> dict:
        """Détail d'un produit : quantité, lots et dates, emplacement, derniers prix.
        produit : nom, code-barres ou identifiant (#12)."""
        return await _call(service.voir_produit, produit)

    @server.tool(title="Voir la liste de courses", annotations=READ)
    async def voir_liste_courses() -> dict:
        """Articles de la liste de courses (produit, quantité, note)."""
        return await _call(service.voir_liste_courses)

    @server.tool(title="Recettes faisables", annotations=READ)
    async def recettes_faisables() -> dict:
        """Recettes et, pour chacune, si le stock permet de la faire et le nombre de produits manquants."""
        return await _call(service.recettes_faisables)

    @server.tool(title="Emplacements et unités", annotations=READ)
    async def voir_references() -> dict:
        """Noms des emplacements (Frigo, Placard…) et des unités de quantité connus de Grocy."""
        return await _call(service.voir_references)

    @server.tool(title="Ajouter à la liste de courses", annotations=ADD)
    async def ajouter_liste_courses(
        produit: str, quantite: float = 1, note: str | None = None, cle_idempotence: str | None = None
    ) -> dict:
        """Ajoute un produit à la liste de courses (la note remplace celle éventuellement présente).
        Une demande identique répétée dans les 10 minutes n'est pas exécutée deux fois, sauf nouvelle cle_idempotence."""
        return await _call(service.ajouter_liste_courses, produit, quantite, note, cle_idempotence)

    @server.tool(title="Retirer de la liste de courses", annotations=CHANGE)
    async def retirer_liste_courses(produit: str, quantite: float = 1, cle_idempotence: str | None = None) -> dict:
        """Diminue la quantité d'un produit sur la liste de courses (le retire s'il n'en reste plus)."""
        return await _call(service.retirer_liste_courses, produit, quantite, cle_idempotence)

    @server.tool(title="Ajouter les manquants à la liste", annotations=ADD)
    async def ajouter_manquants_liste(recette: str | None = None, jeton_confirmation: str | None = None) -> dict:
        """En deux temps. Sans jeton : aperçu des produits sous leur stock minimum (ou des ingrédients manquants de
        `recette`) et jeton_confirmation. Avec le jeton (valable 5 minutes, après accord de la personne) : ajout."""
        return await _call(service.ajouter_manquants_liste, recette, jeton_confirmation)

    @server.tool(title="Enregistrer un achat", annotations=ADD)
    async def enregistrer_achat(
        produit: str,
        quantite: float,
        prix: float | None = None,
        peremption: str | None = None,
        emplacement: str | None = None,
        cle_idempotence: str | None = None,
    ) -> dict:
        """Ajoute au stock un produit acheté. prix : prix unitaire ; peremption : date AAAA-MM-JJ ;
        emplacement : nom (sinon l'emplacement par défaut). Annulable avec annuler_derniere_action."""
        return await _call(service.enregistrer_achat, produit, quantite, prix, peremption, emplacement, cle_idempotence)

    @server.tool(title="Consommer", annotations=CHANGE)
    async def consommer(produit: str, quantite: float, jete: bool = False, cle_idempotence: str | None = None) -> dict:
        """Retire du stock une quantité consommée (jete=true si le produit a été jeté). Annulable."""
        return await _call(service.consommer, produit, quantite, jete, cle_idempotence)

    @server.tool(title="Marquer ouvert", annotations=CHANGE)
    async def ouvrir(produit: str, quantite: float = 1, cle_idempotence: str | None = None) -> dict:
        """Marque une quantité d'un produit comme ouverte. Annulable."""
        return await _call(service.ouvrir, produit, quantite, cle_idempotence)

    @server.tool(title="Déplacer", annotations=CHANGE)
    async def deplacer(produit: str, quantite: float, de: str, vers: str, cle_idempotence: str | None = None) -> dict:
        """Déplace une quantité d'un emplacement (`de`) vers un autre (`vers`). Annulable."""
        return await _call(service.deplacer, produit, quantite, de, vers, cle_idempotence)

    @server.tool(title="Corriger l'inventaire", annotations=CHANGE)
    async def corriger_inventaire(
        produit: str, nouvelle_quantite: float, jeton_confirmation: str | None = None, cle_idempotence: str | None = None
    ) -> dict:
        """Fixe la quantité réelle en stock. Un écart important (plus de 10 unités ou 50 %) renvoie d'abord un aperçu
        et un jeton_confirmation ; rappelez avec ce jeton après accord de la personne. Annulable."""
        return await _call(service.corriger_inventaire, produit, nouvelle_quantite, jeton_confirmation, cle_idempotence)

    @server.tool(title="Annuler la dernière action", annotations=CHANGE)
    async def annuler_derniere_action() -> dict:
        """Annule la dernière action de stock (achat, consommation, ouverture, déplacement, inventaire) faite via ce
        connecteur par ce compte dans les dernières 24 heures. Les modifications de la liste de courses ne s'annulent pas."""
        return await _call(service.annuler_derniere_action)


def build_app(
    settings: Settings,
    *,
    store: Store,
    box: SecretBox,
    audit: AuditLog,
    grocy_transport: httpx2.AsyncBaseTransport | None = None,
    cimd_fetcher: CimdFetcher | None = None,
) -> Starlette:
    def grocy_factory(key: str) -> GrocyClient:
        return GrocyClient(settings.grocy_base_url, key, transport=grocy_transport)

    provider = GrocyOAuthProvider(
        settings, store, ClientPolicy(settings.extra_redirect_uris), cimd_fetcher or CimdFetcher(settings.cimd_hosts), audit
    )
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=settings.resource_url,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    server = MCPServer(
        name="grocy", title="Grocy", instructions=INSTRUCTIONS, version=__version__,
        auth_server_provider=provider, auth=auth,
    )
    register_tools(server, ToolService(settings, store, box, audit, grocy_factory))
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.host],
            allowed_origins=list(settings.allowed_origins),
        ),
    )

    metadata = build_metadata(auth.issuer_url, None, auth.client_registration_options, auth.revocation_options)
    metadata.client_id_metadata_document_supported = True
    metadata.authorization_response_iss_parameter_supported = True
    document = metadata.model_dump(mode="json", exclude_none=True)

    async def authorization_server_metadata(request: Request) -> Response:
        return JSONResponse(document, headers={"Cache-Control": "public, max-age=3600"})

    async def health(request: Request) -> Response:
        return PlainTextResponse("ok")

    app.router.routes.insert(
        0,
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(authorization_server_metadata, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
    )
    app.router.routes.extend(Pages(settings, store, box, provider, grocy_factory, audit).routes())
    app.router.routes.append(Route("/sante", health, methods=["GET"]))
    return app
