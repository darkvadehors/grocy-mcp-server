"""Logique des outils MCP (spec §5) : lecture et action sur le Grocy de la personne connectée, avec garde-fous."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, NoReturn, TypeVar

from grocy_mcp.audit import AuditLog
from grocy_mcp.config import Settings
from grocy_mcp.crypto import CryptoError, SecretBox
from grocy_mcp.grocy import (
    GrocyAuthError,
    GrocyClient,
    GrocyError,
    GrocyForbidden,
    GrocyRequestError,
    GrocyUnavailable,
)
from grocy_mcp.store import Store, sha256_hex

T = TypeVar("T")
MAX_TEXT = 120
MAX_RESULTS = 25
CONFIRMATION_TTL = 300
IDEMPOTENCY_TTL = 600
UNDO_WINDOW = 86_400
_BARCODE = re.compile(r"[0-9]{8,14}")
_PRODUCT_ID = re.compile(r"#?[0-9]{1,7}")
UNAVAILABLE = "Grocy ne répond pas pour le moment. Réessayez dans quelques instants."


class ToolFailure(Exception):
    """Erreur expliquée à la personne ; aucune écriture n'a eu lieu (ou elle a été refusée par Grocy)."""


@dataclass(frozen=True)
class Caller:
    grocy_user_id: int
    family_id: str
    client_id: str


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def clean_text(value: object, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    kept = "".join(ch for ch in str(value) if unicodedata.category(ch)[0] != "C")
    return kept.strip()[:limit]


def _num(value: object) -> float | int:
    number = float(value or 0)
    return int(number) if number.is_integer() else round(number, 3)


def _product_ref(product: dict) -> dict:
    return {"id": int(product["id"]), "nom": clean_text(product.get("name"))}


def _is_normal_recipe(recipe: dict) -> bool:
    return recipe.get("type", "normal") == "normal"


class ToolService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        box: SecretBox,
        audit: AuditLog,
        grocy_factory: Callable[[str], GrocyClient],
    ) -> None:
        self.settings = settings
        self.store = store
        self.box = box
        self.audit = audit
        self.grocy_factory = grocy_factory

    # --- accès à Grocy avec la clé de la personne --------------------------------------------------
    def _client(self, caller: Caller) -> GrocyClient:
        account = self.store.get_account(caller.grocy_user_id)
        if account is None:
            raise ToolFailure("Compte inconnu de ce serveur. Reconnectez le connecteur.")
        try:
            key = self.box.open(account.enc_key, f"grocy-user:{caller.grocy_user_id}")
        except CryptoError:
            raise ToolFailure("Clé Grocy illisible sur ce serveur. Reconnectez le connecteur.") from None
        return self.grocy_factory(key)

    async def _grocy(self, caller: Caller, operation: Callable[[GrocyClient], Awaitable[T]]) -> T:
        client = self._client(caller)
        try:
            return await operation(client)
        except GrocyAuthError:
            await self._confirm_revocation(caller, client)
        except GrocyUnavailable:
            raise ToolFailure(UNAVAILABLE) from None
        except GrocyForbidden as error:
            raise ToolFailure(f"Ce compte Grocy n'a pas la permission nécessaire ({error.message}).") from None
        except GrocyRequestError as error:
            raise ToolFailure(f"Grocy a refusé l'opération : {error.message}") from None

    async def _confirm_revocation(self, caller: Caller, client: GrocyClient) -> NoReturn:
        """Spec §3.8 : un 401 de Grocy est vérifié une seconde fois avant de révoquer les connexions du compte."""
        try:
            await client.current_user()
        except GrocyAuthError:
            count = self.store.revoke_account(caller.grocy_user_id)
            self.audit.write("revocation_cle_grocy", compte_id=caller.grocy_user_id, connexions=count)
            raise ToolFailure(
                "La clé Grocy de ce compte n'est plus valide : la connexion a été révoquée. "
                "Reconnectez le connecteur avec une nouvelle clé."
            ) from None
        except GrocyError:
            raise ToolFailure(UNAVAILABLE) from None
        raise ToolFailure("Grocy a refusé la requête une fois. Réessayez.")

    # --- résolution des noms -------------------------------------------------------------------------
    async def _find_product(self, client: GrocyClient, produit: str) -> tuple[str, Any]:
        text = clean_text(produit)
        if not text:
            raise ToolFailure("Indiquez un produit : nom, code-barres ou identifiant (#12).")
        if _BARCODE.fullmatch(text):
            details = await client.product_by_barcode(text)
            return ("ok", details["product"]) if details else ("introuvable", None)
        products = [p for p in await client.products() if int(p.get("active", 1)) == 1]
        if _PRODUCT_ID.fullmatch(text):
            wanted_id = int(text.lstrip("#"))
            match = [p for p in products if int(p["id"]) == wanted_id]
            return ("ok", match[0]) if match else ("introuvable", None)
        wanted = fold(text)
        exact = [p for p in products if fold(str(p["name"])) == wanted]
        matches = exact or [p for p in products if wanted in fold(str(p["name"]))]
        if len(matches) == 1:
            return "ok", matches[0]
        if not matches:
            return "introuvable", None
        return "ambigu", matches[:MAX_RESULTS]

    @staticmethod
    def _unresolved(status: str, found: Any, produit: str) -> dict:
        if status == "ambigu":
            return {
                "statut": "ambigu",
                "message": "Plusieurs produits correspondent : aucune action. Précisez le nom exact ou l'identifiant (#12).",
                "candidats": [_product_ref(p) for p in found],
            }
        return {"statut": "introuvable", "message": f"Aucun produit ne correspond à « {clean_text(produit)} »."}

    @staticmethod
    def _pick(items: list[dict], name: str) -> list[dict]:
        wanted = fold(clean_text(name))
        exact = [i for i in items if fold(str(i["name"])) == wanted]
        return exact or [i for i in items if wanted and wanted in fold(str(i["name"]))]

    def _match_location(self, locations: list[dict], name: str) -> dict:
        matches = self._pick(locations, name)
        if len(matches) != 1:
            names = ", ".join(clean_text(loc["name"]) for loc in locations)
            raise ToolFailure(f"Emplacement « {clean_text(name)} » introuvable ou ambigu. Emplacements : {names}.")
        return matches[0]

    async def _find_location(self, client: GrocyClient, name: str) -> dict:
        return self._match_location(await client.locations(), name)

    # --- outils de lecture ---------------------------------------------------------------------------
    async def rechercher_produit(self, caller: Caller, texte: str) -> dict:
        async def run(client: GrocyClient) -> dict:
            text = clean_text(texte)
            if _BARCODE.fullmatch(text):
                details = await client.product_by_barcode(text)
                return {"produits": [_product_ref(details["product"])] if details else [], "tronque": False}
            wanted = fold(text)
            if not wanted:
                raise ToolFailure("Indiquez un texte à rechercher.")
            found = [
                p for p in await client.products() if int(p.get("active", 1)) == 1 and wanted in fold(str(p["name"]))
            ]
            return {"produits": [_product_ref(p) for p in found[:MAX_RESULTS]], "tronque": len(found) > MAX_RESULTS}

        return await self._grocy(caller, run)

    async def voir_stock(self, caller: Caller, emplacement: str | None = None) -> dict:
        async def run(client: GrocyClient) -> dict:
            rows = await client.stock()
            location = await self._find_location(client, emplacement) if emplacement else None
            items = []
            for row in rows:
                product = row.get("product") or {}
                if location and int(product.get("location_id") or 0) != int(location["id"]):
                    continue
                items.append(
                    {
                        "id": int(row["product_id"]),
                        "nom": clean_text(product.get("name")),
                        "quantite": _num(row.get("amount")),
                        "ouverts": _num(row.get("amount_opened")),
                        "prochaine_peremption": row.get("best_before_date"),
                    }
                )
            items.sort(key=lambda item: fold(item["nom"]))
            return {"emplacement": clean_text(location["name"]) if location else None, "produits": items}

        return await self._grocy(caller, run)

    async def voir_peremptions(self, caller: Caller, jours: int = 5) -> dict:
        if not isinstance(jours, int) or isinstance(jours, bool) or not 1 <= jours <= 60:
            raise ToolFailure("jours doit être un entier entre 1 et 60.")

        async def run(client: GrocyClient) -> dict:
            data = await client.volatile(jours)

            def rows(key: str) -> list[dict]:
                return [
                    {
                        "id": int(r["product_id"]),
                        "nom": clean_text((r.get("product") or {}).get("name")),
                        "quantite": _num(r.get("amount")),
                        "date": r.get("best_before_date"),
                    }
                    for r in data.get(key) or []
                ]

            missing = [
                {"id": int(m["id"]), "nom": clean_text(m.get("name")), "manquant": _num(m.get("amount_missing"))}
                for m in data.get("missing_products") or []
            ]
            return {
                "jours": jours,
                "bientot": rows("due_products"),
                "depasses": rows("overdue_products"),
                "perimes": rows("expired_products"),
                "sous_le_minimum": missing,
            }

        return await self._grocy(caller, run)

    async def voir_produit(self, caller: Caller, produit: str) -> dict:
        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            product_id = int(found["id"])
            details = await client.product_details(product_id)
            entries = await client.product_entries(product_id)
            prices = await client.price_history(product_id)
            return {
                "statut": "ok",
                "produit": _product_ref(found),
                "quantite": _num(details.get("stock_amount")),
                "ouverts": _num(details.get("stock_amount_opened")),
                "prochaine_peremption": details.get("next_due_date"),
                "emplacement_par_defaut": clean_text((details.get("location") or {}).get("name")) or None,
                "dernier_prix": details.get("last_price"),
                "lots": [
                    {"quantite": _num(e.get("amount")), "peremption": e.get("best_before_date"), "emplacement_id": e.get("location_id")}
                    for e in entries
                    if float(e.get("amount") or 0) > 0
                ][:MAX_RESULTS],
                "historique_prix": [{"date": h.get("date"), "prix": h.get("price")} for h in prices[:5]],
            }

        return await self._grocy(caller, run)

    async def voir_liste_courses(self, caller: Caller) -> dict:
        async def run(client: GrocyClient) -> dict:
            items = await client.shopping_list()
            names = {int(p["id"]): p.get("name") for p in await client.products()}
            return {
                "articles": [
                    {
                        "id": int(i["id"]),
                        "produit_id": int(i["product_id"]) if i.get("product_id") is not None else None,
                        "produit": clean_text(names.get(int(i["product_id"]))) if i.get("product_id") is not None else None,
                        "quantite": _num(i.get("amount")),
                        "note": clean_text(i.get("note"), 200) or None,
                    }
                    for i in items
                ]
            }

        return await self._grocy(caller, run)

    async def recettes_faisables(self, caller: Caller) -> dict:
        async def run(client: GrocyClient) -> dict:
            fulfillment = await client.recipes_fulfillment()
            names = {int(r["id"]): r.get("name") for r in await client.recipes() if _is_normal_recipe(r)}
            return {
                "recettes": [
                    {
                        "recette_id": int(f["recipe_id"]),
                        "nom": clean_text(names[int(f["recipe_id"])]),
                        "faisable": bool(int(f.get("need_fulfilled") or 0)),
                        "produits_manquants": int(f.get("missing_products_count") or 0),
                    }
                    for f in fulfillment
                    if int(f["recipe_id"]) in names
                ]
            }

        return await self._grocy(caller, run)

    async def voir_references(self, caller: Caller) -> dict:
        async def run(client: GrocyClient) -> dict:
            locations = await client.locations()
            units = await client.quantity_units()
            return {
                "emplacements": [clean_text(loc["name"]) for loc in locations],
                "unites": [clean_text(u["name"]) for u in units],
            }

        return await self._grocy(caller, run)
