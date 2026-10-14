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
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, NoReturn, TypeVar

import anyio

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
from grocy_mcp.nutrition import CiqualIndex, check_values, daily_needs
from grocy_mcp.packaging import metric_kind, own_conversions, resolve_unit
from grocy_mcp.packaging_job import JobState, OffCache, decide, describe, load_catalog, needs_lookup
from grocy_mcp.sources import FoodSources, SourceError
from grocy_mcp.store import Store, sha256_hex

T = TypeVar("T")
MAX_TEXT = 120
MAX_RESULTS = 25
MAX_NAME = 60
MAX_IMPORT = 30
NEVER = "2999-12-31"  # « jamais » dans Grocy
CONFIRMATION_TTL = 300
IDEMPOTENCY_TTL = 600
UNDO_WINDOW = 86_400
_BARCODE = re.compile(r"[0-9]{8,14}")
_PRODUCT_ID = re.compile(r"#?[0-9]{1,7}")
_PENDING = "__en_cours__"
_UNCERTAIN = "__incertain__"
NUTRI_FIELDS = {
    "kcal": "nutri_kcal_100g",
    "proteines": "nutri_proteines_100g",
    "glucides": "nutri_glucides_100g",
    "sucres": "nutri_sucres_100g",
    "lipides": "nutri_lipides_100g",
    "ags": "nutri_ags_100g",
    "fibres": "nutri_fibres_100g",
    "sel": "nutri_sel_100g",
}
NUTRI_WEIGHT, NUTRI_SOURCE, NUTRI_NON_FOOD = "nutri_poids_unite_g", "nutri_source", "nutri_non_alimentaire"
PRODUCT_LOOKUP_SECONDS = 25
# Unités de stock de masse ou de volume (nom replié) → (grammes par unité, approximatif ?)
_UNIT_GRAMS = {
    "g": (1, False), "gr": (1, False), "gramme": (1, False), "grammes": (1, False), "gram": (1, False),
    "kg": (1000, False), "kilo": (1000, False), "kilos": (1000, False), "kilogramme": (1000, False),
    "kilogrammes": (1000, False), "ml": (1, True), "millilitre": (1, True), "millilitres": (1, True),
    "cl": (10, True), "l": (1000, True), "litre": (1000, True), "litres": (1000, True),
}
_SLOW = object()  # recherche externe trop lente (aperçu d'import)
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


def _to_number(value: object) -> float | int | None:
    """Valeur numérique d'un champ Grocy (texte) ; None si vide ou illisible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number == int(number) else number


def _read_key(path) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _whole_words(wanted: str, name: str) -> bool:
    """Vrai si `wanted` apparaît dans `name` en mots entiers (« yaourt » dans « yaourt nature », pas « riz » dans « chorizo »)."""
    return bool(wanted) and re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", name) is not None


def _is_normal_recipe(recipe: dict) -> bool:
    return recipe.get("type", "normal") == "normal"



_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MAX_PRICE = 10_000
MAX_INVENTORY = 10_000


def _fmt(value: float) -> str:
    return str(_num(value)).replace(".", ",")


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)

class ToolService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        box: SecretBox,
        audit: AuditLog,
        grocy_factory: Callable[[str], GrocyClient],
        *,
        sources: FoodSources | None = None,
        ciqual: CiqualIndex | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.box = box
        self.audit = audit
        self.grocy_factory = grocy_factory
        self.sources = sources or FoodSources(usda_key=_read_key(settings.usda_key_path))
        self._ciqual = ciqual
        self._off_cache = OffCache(None)  # contenances Open Food Facts déjà lues (mémoire du processus)

    @property
    def ciqual(self) -> CiqualIndex:
        if self._ciqual is None:  # chargée au premier usage (≈ 0,5 Mo)
            self._ciqual = CiqualIndex()
        return self._ciqual

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
            raise ToolFailure(f"Ce compte Grocy n'a pas la permission nécessaire ({clean_text(error.message, 200)}).") from None
        except GrocyRequestError as error:
            raise ToolFailure(f"Grocy a refusé l'opération : {clean_text(error.message, 200)}") from None

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
        wanted = fold(text)
        exact = [p for p in products if fold(str(p["name"])) == wanted]
        if not exact and _PRODUCT_ID.fullmatch(text):  # un produit nommé « 1664 » l'emporte sur l'identifiant 1664
            wanted_id = int(text.lstrip("#"))
            match = [p for p in products if int(p["id"]) == wanted_id]
            return ("ok", match[0]) if match else ("introuvable", None)
        matches = exact or [p for p in products if _whole_words(wanted, fold(str(p["name"])))]
        if len(matches) == 1:
            return "ok", matches[0]
        if not matches:
            # une correspondance au milieu d'un mot (« riz » dans « Chorizo ») n'est qu'une suggestion, jamais une cible
            partial = [p for p in products if wanted in fold(str(p["name"]))]
            return ("ambigu", partial[:MAX_RESULTS]) if partial else ("introuvable", None)
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
        return exact or [i for i in items if wanted and _whole_words(wanted, fold(str(i["name"])))]

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
            groups = await client.product_groups()
            return {
                "emplacements": [clean_text(loc["name"]) for loc in locations],
                "unites": [clean_text(u["name"]) for u in units],
                "categories": [clean_text(g["name"]) for g in groups],
            }

        return await self._grocy(caller, run)

    # --- garde-fous communs aux actions --------------------------------------------------------------
    def _quantity(self, value: object, name: str = "quantite") -> float:
        if not _is_number(value) or value <= 0:  # type: ignore[operator]
            raise ToolFailure(f"{name} doit être un nombre strictement positif.")
        if value > self.settings.max_quantity:  # type: ignore[operator]
            raise ToolFailure(f"{name} est limitée à {self.settings.max_quantity} par action ; au-delà, passez par Grocy.")
        return float(value)  # type: ignore[arg-type]

    @staticmethod
    def _price(value: object) -> float | None:
        if value is None:
            return None
        if not _is_number(value) or not 0 <= value <= MAX_PRICE:  # type: ignore[operator]
            raise ToolFailure(f"prix doit être un nombre entre 0 et {MAX_PRICE}.")
        return float(value)  # type: ignore[arg-type]

    @staticmethod
    def _date(value: str | None) -> str | None:
        if value is None:
            return None
        text = clean_text(value, 10)
        try:
            parsed = date.fromisoformat(text) if _ISO_DATE.fullmatch(text) else None
        except ValueError:
            parsed = None
        if parsed is None or not 2000 <= parsed.year <= 2999:
            raise ToolFailure("peremption doit être une date au format AAAA-MM-JJ.")
        return parsed.isoformat()

    def _quota_left(self, caller: Caller) -> int:
        now, uid = self.store.now(), caller.grocy_user_id
        return max(
            0,
            min(
                self.settings.actions_per_minute - self.store.count_actions(uid, now - 60),
                self.settings.actions_per_hour - self.store.count_actions(uid, now - 3600),
            ),
        )

    def _consume_quota(self, caller: Caller, count: int = 1) -> None:
        now, uid = self.store.now(), caller.grocy_user_id
        if self.store.count_actions(uid, now - 60) + count > self.settings.actions_per_minute:
            raise ToolFailure(
                f"Limite de {self.settings.actions_per_minute} actions par minute atteinte. Réessayez dans une minute."
            )
        if self.store.count_actions(uid, now - 3600) + count > self.settings.actions_per_hour:
            raise ToolFailure(f"Limite de {self.settings.actions_per_hour} actions par heure atteinte. Réessayez plus tard.")
        for _ in range(count):
            self.store.record_action(uid)

    async def _execute(
        self, caller: Caller, tool: str, args: dict, run: Callable[[GrocyClient], Awaitable[dict]], count: int = 1
    ) -> dict:
        self._consume_quota(caller, count)
        base = {"compte_id": caller.grocy_user_id, "client": caller.client_id, "outil": tool, "parametres": args}
        try:
            result = await self._grocy(caller, run)
        except ToolFailure as failure:
            self.audit.write("action", **base, statut="erreur", erreur=str(failure))
            raise
        self.audit.write("action", **base, statut=result.get("statut"), transaction=result.get("transaction_id"))
        return result

    async def _action(
        self,
        caller: Caller,
        tool: str,
        args: dict,
        cle: str | None,
        run: Callable[[GrocyClient], Awaitable[dict]],
    ) -> dict:
        canonical = json.dumps([tool, args], sort_keys=True, ensure_ascii=False)
        key = clean_text(cle, 100) if cle else hashlib.sha256(canonical.encode()).hexdigest()
        slot = f"{tool}:{key}"
        previous = self.store.reserve_idempotent(caller.grocy_user_id, slot, IDEMPOTENCY_TTL, _PENDING)
        if previous == _PENDING:
            return {
                "statut": "en_cours",
                "message": "Une action identique est déjà en cours : elle n'est pas lancée une seconde fois.",
            }
        if previous == _UNCERTAIN:
            return {
                "statut": "incertain",
                "message": "Une action identique a été envoyée il y a moins de 10 minutes mais Grocy n'a pas répondu : "
                "elle a peut-être été enregistrée. Vérifiez (voir_produit, voir_liste_courses) avant de recommencer ; "
                "pour la refaire volontairement, fournissez une nouvelle cle_idempotence.",
            }
        if previous is not None:
            earlier = json.loads(previous)
            return {
                **earlier,
                "deja_execute": True,
                "message": "Action identique déjà exécutée il y a moins de 10 minutes, non répétée "
                f"({earlier.get('message', '')}). Pour la refaire volontairement, fournissez une nouvelle cle_idempotence.",
            }
        try:
            result = await self._execute(caller, tool, args, run)
        except ToolFailure as failure:
            if str(failure) == UNAVAILABLE:
                # Grocy a pu enregistrer l'écriture avant que la réponse ne se perde : ne pas la rejouer à l'aveugle
                self.store.save_idempotent(caller.grocy_user_id, slot, _UNCERTAIN)
            else:
                self.store.release_idempotent(caller.grocy_user_id, slot)
            raise
        except BaseException:
            self.store.release_idempotent(caller.grocy_user_id, slot)
            raise
        if result.get("statut") == "ok":
            self.store.save_idempotent(caller.grocy_user_id, slot, json.dumps(result, ensure_ascii=False))
        else:
            self.store.release_idempotent(caller.grocy_user_id, slot)
        return result

    def _stock_done(self, caller: Caller, tool: str, rows: list[dict] | None, summary: str) -> dict:
        transaction = str(rows[0]["transaction_id"]) if rows else None
        if transaction:
            self.store.record_undo(caller.grocy_user_id, transaction, tool, summary)
        return {
            "statut": "ok",
            "message": summary[:1].upper() + summary[1:] + ".",
            "transaction_id": transaction,
            "annulable": transaction is not None,
        }

    def _new_confirmation(self, caller: Caller, tool: str, payload: dict) -> str:
        token = secrets.token_urlsafe(16)
        self.store.save_confirmation(
            sha256_hex(token), caller.grocy_user_id, tool, json.dumps(payload), self.store.now() + CONFIRMATION_TTL
        )
        return token

    def _take_confirmation(self, caller: Caller, tool: str, token: str) -> dict:
        payload = self.store.pop_confirmation(sha256_hex(clean_text(token, 100)), caller.grocy_user_id, tool)
        if payload is None:
            raise ToolFailure("Jeton de confirmation invalide, déjà utilisé ou expiré (5 minutes). Redemandez un aperçu.")
        return json.loads(payload)

    # --- liste de courses ----------------------------------------------------------------------------
    async def ajouter_liste_courses(
        self, caller: Caller, produit: str, quantite: float = 1, note: str | None = None, cle_idempotence: str | None = None
    ) -> dict:
        amount = self._quantity(quantite)
        note_text = clean_text(note, 200) or None

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            await client.add_to_shopping_list(int(found["id"]), amount, note_text)
            return {
                "statut": "ok",
                "message": f"{_fmt(amount)} × {clean_text(found['name'])} ajouté à la liste de courses.",
                "produit": _product_ref(found),
            }

        args = {"produit": clean_text(produit), "quantite": amount, "note": note_text}
        return await self._action(caller, "ajouter_liste_courses", args, cle_idempotence, run)

    async def retirer_liste_courses(
        self, caller: Caller, produit: str, quantite: float = 1, cle_idempotence: str | None = None
    ) -> dict:
        amount = self._quantity(quantite)

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            product_id = int(found["id"])
            listed = [
                i for i in await client.shopping_list() if i.get("product_id") is not None and int(i["product_id"]) == product_id
            ]
            if not listed:
                return {"statut": "absent", "message": f"{clean_text(found['name'])} n'est pas sur la liste de courses."}
            await client.remove_from_shopping_list(product_id, amount)
            return {
                "statut": "ok",
                "message": f"{_fmt(amount)} × {clean_text(found['name'])} retiré de la liste de courses.",
                "produit": _product_ref(found),
            }

        args = {"produit": clean_text(produit), "quantite": amount}
        return await self._action(caller, "retirer_liste_courses", args, cle_idempotence, run)

    async def ajouter_manquants_liste(
        self, caller: Caller, recette: str | None = None, jeton_confirmation: str | None = None
    ) -> dict:
        tool = "ajouter_manquants_liste"
        if jeton_confirmation:
            data = self._take_confirmation(caller, tool, jeton_confirmation)

            async def confirmed(client: GrocyClient) -> dict:
                if data["recette_id"] is None:
                    await client.add_missing_products()
                else:
                    await client.add_recipe_missing_products(int(data["recette_id"]))
                return {"statut": "ok", "message": "Produits manquants ajoutés à la liste de courses."}

            return await self._execute(caller, tool, {"recette_id": data["recette_id"], "confirmation": True}, confirmed)

        async def preview(client: GrocyClient) -> dict:
            if recette:
                recipes = [r for r in await client.recipes() if _is_normal_recipe(r)]
                matches = self._pick(recipes, recette)
                if len(matches) != 1:
                    if not matches:
                        return {"statut": "introuvable", "message": f"Aucune recette ne correspond à « {clean_text(recette)} »."}
                    return {
                        "statut": "ambigu",
                        "message": "Plusieurs recettes correspondent : précisez.",
                        "candidats": [{"id": int(r["id"]), "nom": clean_text(r["name"])} for r in matches[:MAX_RESULTS]],
                    }
                recipe_id = int(matches[0]["id"])
                state = next((f for f in await client.recipes_fulfillment() if int(f["recipe_id"]) == recipe_id), {})
                count = int(state.get("missing_products_count") or 0)
                if count == 0:
                    return {"statut": "rien_a_ajouter", "message": "Tous les ingrédients de cette recette sont en stock."}
                apercu: dict = {
                    "recette": clean_text(matches[0]["name"]),
                    "produits_manquants": count,
                    "ingredients": clean_text(state.get("product_names_comma_separated"), 500) or None,
                }
                payload = {"recette_id": recipe_id}
            else:
                missing = (await client.volatile(5)).get("missing_products") or []
                if not missing:
                    return {"statut": "rien_a_ajouter", "message": "Aucun produit n'est sous son stock minimum."}
                apercu = {"produits": [{"nom": clean_text(m.get("name")), "quantite": _num(m.get("amount_missing"))} for m in missing]}
                payload = {"recette_id": None}
            return {
                "statut": "confirmation_requise",
                "message": "Aperçu : rien n'a encore été ajouté. Montrez cet aperçu à la personne, puis rappelez "
                "ajouter_manquants_liste avec jeton_confirmation pour confirmer.",
                "apercu": apercu,
                "jeton_confirmation": self._new_confirmation(caller, tool, payload),
            }

        return await self._grocy(caller, preview)

    # --- stock ---------------------------------------------------------------------------------------
    async def enregistrer_achat(
        self,
        caller: Caller,
        produit: str,
        quantite: float,
        prix: float | None = None,
        peremption: str | None = None,
        emplacement: str | None = None,
        cle_idempotence: str | None = None,
    ) -> dict:
        amount, price, best_before = self._quantity(quantite), self._price(prix), self._date(peremption)

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            location = await self._find_location(client, emplacement) if emplacement else None
            rows = await client.purchase(
                int(found["id"]), amount, price=price, best_before_date=best_before,
                location_id=int(location["id"]) if location else None,
            )
            return self._stock_done(caller, "enregistrer_achat", rows, f"achat de {_fmt(amount)} × {clean_text(found['name'])} enregistré")

        args = {"produit": clean_text(produit), "quantite": amount, "prix": price, "peremption": best_before, "emplacement": clean_text(emplacement) or None}
        return await self._action(caller, "enregistrer_achat", args, cle_idempotence, run)

    async def consommer(
        self, caller: Caller, produit: str, quantite: float, jete: bool = False, cle_idempotence: str | None = None
    ) -> dict:
        amount, spoiled = self._quantity(quantite), bool(jete)

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            rows = await client.consume(int(found["id"]), amount, spoiled=spoiled)
            verb = "jeté" if spoiled else "consommé"
            return self._stock_done(caller, "consommer", rows, f"{_fmt(amount)} × {clean_text(found['name'])} {verb}")

        args = {"produit": clean_text(produit), "quantite": amount, "jete": spoiled}
        return await self._action(caller, "consommer", args, cle_idempotence, run)

    async def ouvrir(self, caller: Caller, produit: str, quantite: float = 1, cle_idempotence: str | None = None) -> dict:
        amount = self._quantity(quantite)

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            rows = await client.open_product(int(found["id"]), amount)
            return self._stock_done(caller, "ouvrir", rows, f"{_fmt(amount)} × {clean_text(found['name'])} marqué ouvert")

        return await self._action(caller, "ouvrir", {"produit": clean_text(produit), "quantite": amount}, cle_idempotence, run)

    async def deplacer(
        self, caller: Caller, produit: str, quantite: float, de: str, vers: str, cle_idempotence: str | None = None
    ) -> dict:
        amount = self._quantity(quantite)

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            locations = await client.locations()
            source, target = self._match_location(locations, de), self._match_location(locations, vers)
            if int(source["id"]) == int(target["id"]):
                raise ToolFailure("Les emplacements de départ et d'arrivée sont identiques.")
            rows = await client.transfer(int(found["id"]), amount, int(source["id"]), int(target["id"]))
            summary = f"{_fmt(amount)} × {clean_text(found['name'])} déplacé de {clean_text(source['name'])} vers {clean_text(target['name'])}"
            return self._stock_done(caller, "deplacer", rows, summary)

        args = {"produit": clean_text(produit), "quantite": amount, "de": clean_text(de), "vers": clean_text(vers)}
        return await self._action(caller, "deplacer", args, cle_idempotence, run)

    async def corriger_inventaire(
        self,
        caller: Caller,
        produit: str,
        nouvelle_quantite: float,
        jeton_confirmation: str | None = None,
        cle_idempotence: str | None = None,
    ) -> dict:
        tool = "corriger_inventaire"
        if jeton_confirmation:
            data = self._take_confirmation(caller, tool, jeton_confirmation)

            async def confirmed(client: GrocyClient) -> dict:
                details = await client.product_details(int(data["product_id"]))
                if abs(float(details.get("stock_amount") or 0) - data["actuel"]) > 1e-9:
                    raise ToolFailure("Le stock de ce produit a changé depuis l'aperçu : redemandez un aperçu.")
                rows = await client.inventory(int(data["product_id"]), data["nouveau"])
                summary = f"inventaire de {data['nom']} corrigé : {_fmt(data['actuel'])} → {_fmt(data['nouveau'])}"
                return self._stock_done(caller, tool, rows, summary)

            args = {"produit_id": data["product_id"], "nouveau": data["nouveau"], "confirmation": True}
            return await self._execute(caller, tool, args, confirmed)

        if not _is_number(nouvelle_quantite) or not 0 <= nouvelle_quantite <= MAX_INVENTORY:  # type: ignore[operator]
            raise ToolFailure(f"nouvelle_quantite doit être un nombre entre 0 et {MAX_INVENTORY}.")
        new = float(nouvelle_quantite)  # type: ignore[arg-type]

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            product_id, name = int(found["id"]), clean_text(found["name"])
            current = float((await client.product_details(product_id)).get("stock_amount") or 0)
            if abs(new - current) < 1e-9:
                return {"statut": "deja_a_jour", "message": f"{name} est déjà à {_fmt(new)} : rien à corriger."}
            gap = abs(new - current)
            if gap > self.settings.max_quantity:
                raise ToolFailure(
                    f"Écart de {_fmt(gap)} supérieur à la limite de {self.settings.max_quantity} par action : corrigez dans Grocy."
                )
            if (
                gap > self.settings.inventory_abs_threshold
                or current == 0
                or gap * 100 > self.settings.inventory_pct_threshold * current
            ):
                payload = {"product_id": product_id, "nom": name, "actuel": current, "nouveau": new}
                return {
                    "statut": "confirmation_requise",
                    "message": "Écart important : rien n'a été modifié. Montrez cet aperçu à la personne, puis rappelez "
                    "corriger_inventaire avec jeton_confirmation pour confirmer.",
                    "apercu": {"produit": name, "actuel": _num(current), "nouveau": _num(new), "ecart": _num(new - current)},
                    "jeton_confirmation": self._new_confirmation(caller, tool, payload),
                }
            rows = await client.inventory(product_id, new)
            return self._stock_done(caller, tool, rows, f"inventaire de {name} corrigé : {_fmt(current)} → {_fmt(new)}")

        return await self._action(caller, tool, {"produit": clean_text(produit), "nouveau": new}, cle_idempotence, run)

    # --- annulation ----------------------------------------------------------------------------------
    async def annuler_derniere_action(self, caller: Caller) -> dict:
        last = self.store.last_undo(caller.grocy_user_id)
        if last is None or last.undone or self.store.now() - last.at > UNDO_WINDOW:
            return {
                "statut": "rien_a_annuler",
                "message": "Aucune action de stock faite via ce connecteur dans les dernières 24 heures n'est à annuler.",
            }

        transactions = last.transaction_id.split()  # un import enregistre plusieurs transactions

        async def run(client: GrocyClient) -> dict:
            pending = []
            for transaction in transactions:
                rows = await client.transaction(transaction)
                if any(int(r.get("user_id", -1)) != caller.grocy_user_id for r in rows):
                    raise ToolFailure("Cette transaction n'appartient pas à ce compte : annulation refusée.")
                if not all(int(r.get("undone") or 0) for r in rows):
                    pending.append(transaction)
            if not pending:
                self.store.mark_undone(last.rowid)
                return {"statut": "deja_annule", "message": f"Cette action était déjà annulée : {last.summary}."}
            failed, reason = [], ""
            for transaction in reversed(pending):
                try:
                    await client.undo(transaction)
                except GrocyRequestError as error:
                    failed.append(transaction)
                    reason = clean_text(error.message, 200)
            if failed and len(failed) == len(pending):
                raise ToolFailure(f"Annulation impossible : le stock a changé depuis cette action ({reason}).")
            note = ""
            if last.tool == "importer_articles":
                note = " Les produits, emplacements et catégories créés par l'import restent dans Grocy."
                self.store.release_idempotent_prefix(caller.grocy_user_id, "importer_articles:")
            if failed:  # on ne garde que les transactions restantes : un nouvel essai ne vise qu'elles
                self.store.update_undo(last.rowid, " ".join(reversed(failed)))
                return {
                    "statut": "partiel",
                    "message": f"Annulation partielle : {last.summary}. {len(pending) - len(failed)} achat(s) annulé(s), "
                    f"{len(failed)} impossible(s) pour l'instant ({reason}). Relancez annuler_derniere_action plus tard.{note}",
                    "annulees": len(pending) - len(failed),
                    "non_annulees": len(failed),
                    "outil_annule": last.tool,
                }
            self.store.mark_undone(last.rowid)
            return {"statut": "ok", "message": f"Action annulée : {last.summary}.{note}", "outil_annule": last.tool}

        return await self._execute(caller, "annuler_derniere_action", {"transaction": last.transaction_id}, run)

    # --- données de base : création seulement (aucun outil ne modifie ni ne supprime) -----------------
    @staticmethod
    def _master_name(nom: object) -> str:
        if not isinstance(nom, str):
            raise ToolFailure("Le nom doit être un texte.")
        text = clean_text(nom, 200).strip()
        if not text or len(text) > MAX_NAME:
            raise ToolFailure(f"Le nom doit contenir de 1 à {MAX_NAME} caractères.")
        return text

    async def _create_master(
        self, caller: Caller, tool: str, entity: str, label: str, nom: str, extra: dict, cle: str | None
    ) -> dict:
        name = self._master_name(nom)

        async def run(client: GrocyClient) -> dict:
            rows = await (client.locations() if entity == "locations" else client.product_groups())
            same = [r for r in rows if fold(str(r["name"])) == fold(name)]
            if same:
                return {
                    "statut": "existe_deja",
                    "cree": False,
                    "id": int(same[0]["id"]),
                    "message": f"{label} « {clean_text(same[0]['name'])} » existe déjà : rien n'a été créé.",
                }
            new_id = await client.create_object(entity, {"name": name, **extra})
            self.audit.write("creation", compte_id=caller.grocy_user_id, type=entity, id=new_id, nom=name)
            return {"statut": "ok", "cree": True, "id": new_id, "message": f"{label} « {name} » créé(e)."}

        return await self._action(caller, tool, {"nom": name, **extra}, cle, run)

    async def creer_emplacement(
        self, caller: Caller, nom: str, congelateur: bool = False, cle_idempotence: str | None = None
    ) -> dict:
        return await self._create_master(
            caller, "creer_emplacement", "locations", "Emplacement", nom, {"is_freezer": 1 if congelateur else 0}, cle_idempotence
        )

    async def creer_categorie(self, caller: Caller, nom: str, cle_idempotence: str | None = None) -> dict:
        return await self._create_master(caller, "creer_categorie", "product_groups", "Catégorie", nom, {}, cle_idempotence)

    # --- importer_articles (spec E2 §2.1) -------------------------------------------------------------
    def _import_line(self, raw: object) -> dict:
        """Valide une ligne ; renvoie ses champs nettoyés, ou {'erreur': raison}."""
        if not isinstance(raw, dict):
            return {"erreur": "ligne illisible (objet attendu)"}
        code = raw.get("code_barres")
        code = str(code).strip() if code not in (None, "") else None
        nom = clean_text(raw.get("nom"), MAX_TEXT) if raw.get("nom") not in (None, "") else None
        if code is not None and not _BARCODE.fullmatch(code):
            return {"erreur": "code-barres invalide (8 à 14 chiffres)"}
        if code is None and not nom:
            return {"erreur": "ni code-barres ni nom"}
        quantite = raw.get("quantite", 1)
        if not _is_number(quantite) or not 0 < quantite <= self.settings.max_quantity:  # type: ignore[operator]
            return {"erreur": f"quantite invalide (entre 0 et {self.settings.max_quantity})"}
        when = raw.get("date_peremption")
        if when not in (None, ""):
            text = str(when).strip()
            try:
                parsed = date.fromisoformat(text) if _ISO_DATE.fullmatch(text) else None
            except ValueError:
                parsed = None
            if parsed is None:
                return {"erreur": "date_peremption invalide (format AAAA-MM-JJ)"}
            when = parsed.isoformat()
        else:
            when = None
        warning = None
        today = datetime.fromtimestamp(self.store.now(), ZoneInfo(self.settings.timezone)).date().isoformat()
        if when == today:
            # FoodShiner et d'autres applications mettent la date du jour quand aucune péremption n'a été saisie
            when = None
            warning = "date de péremption égale au jour de l'import (souvent : aucune date saisie) : « jamais » retenu, à vérifier"
        place = clean_text(raw.get("emplacement"), MAX_NAME) if raw.get("emplacement") not in (None, "") else None
        group = clean_text(raw.get("categorie"), MAX_NAME) if raw.get("categorie") not in (None, "") else None
        return {
            "code": code,
            "nom": nom,
            "quantite": _num(quantite),
            "date": when,
            "emplacement": place,
            "categorie": group,
            "avertissement": warning,
        }

    @staticmethod
    def _by_name(rows: list[dict], name: str | None) -> dict | None:
        if not name:
            return None
        return next((r for r in rows if fold(str(r["name"])) == fold(name)), None)

    async def importer_articles(self, caller: Caller, articles: object, jeton_confirmation: str | None = None) -> dict:
        tool = "importer_articles"
        if not isinstance(articles, list) or not articles:
            raise ToolFailure("articles doit être une liste non vide d'articles.")
        if len(articles) > MAX_IMPORT:
            raise ToolFailure(f"{MAX_IMPORT} articles au plus par import : découpez la liste en plusieurs appels.")
        lines = [self._import_line(raw) for raw in articles]
        digest = hashlib.sha256(json.dumps(lines, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        slot = f"{tool}:{digest}"
        if jeton_confirmation:
            payload = self._take_confirmation(caller, tool, jeton_confirmation)
            if payload.get("empreinte") != digest:
                raise ToolFailure(
                    "Liste différente de celle de l'aperçu : rien n'a été importé. Renvoyez exactement les mêmes articles "
                    "avec ce jeton, ou redemandez un aperçu."
                )
            return await self._import_confirm(caller, payload, slot)
        previous = self.store.get_idempotent(caller.grocy_user_id, slot, IDEMPOTENCY_TTL)
        if previous is not None:
            return self._import_already(previous)
        return await self._grocy(caller, lambda client: self._import_preview(caller, client, lines, digest))

    @staticmethod
    def _import_already(previous: str) -> dict:
        if previous == _PENDING:
            return {"statut": "en_cours", "message": "Cet import est déjà en cours : il n'est pas relancé."}
        if previous == _UNCERTAIN:
            return {
                "statut": "incertain",
                "message": "Cet import a été interrompu il y a moins de 10 minutes : une partie a peut-être été enregistrée. "
                "Vérifiez le stock (voir_stock, voir_produit) avant de recommencer ; annuler_derniere_action annule "
                "les achats déjà faits.",
            }
        return {
            "statut": "deja_importe",
            "message": "Cette même liste a déjà été importée il y a moins de 10 minutes : rien n'a été refait. "
            "Si c'est voulu (nouvel achat identique), attendez ou modifiez la liste.",
            "bilan": json.loads(previous).get("lignes", []),
        }

    async def _import_lookups(self, client: GrocyClient, lines: list[dict]) -> tuple[dict, dict]:
        """Codes déjà connus de Grocy, puis recherche multi-sources des autres, 4 à la fois."""
        known: dict[str, dict] = {}
        for code in dict.fromkeys(line["code"] for line in lines if "erreur" not in line and line["code"]):
            details = await client.product_by_barcode(code)
            if details:
                known[code] = details["product"]
        unknown = [c for c in dict.fromkeys(line.get("code") for line in lines if "erreur" not in line) if c and c not in known]
        found: dict[str, Any] = {}
        limit = anyio.Semaphore(4)

        async def one(code: str) -> None:
            async with limit:
                try:
                    found[code] = await client.external_lookup(code)
                except GrocyUnavailable:
                    found[code] = _SLOW

        async with anyio.create_task_group() as group:
            for code in unknown:
                group.start_soon(one, code)
        return known, found

    async def _import_preview(self, caller: Caller, client: GrocyClient, lines: list[dict], digest: str) -> dict:
        products = [p for p in await client.products() if int(p.get("active", 1)) == 1]
        locations, groups = await client.locations(), await client.product_groups()
        known, found = await self._import_lookups(client, lines)
        out, plan, new_places, new_groups = [], [], {}, {}
        seen_codes: dict[str, int] = {}
        for number, line in enumerate(lines, start=1):
            if "erreur" in line:
                out.append({"ligne": number, "statut": "ignore", "raison": line["erreur"]})
                continue
            entry = {"ligne": number, "quantite": line["quantite"], "date_peremption": line["date"] or NEVER}
            if line.get("avertissement"):
                entry["avertissement"] = line["avertissement"]
            step: dict = {"quantite": line["quantite"], "date": line["date"] or NEVER, "code": line["code"]}
            if line["code"] and line["code"] in seen_codes:  # même code plus haut dans la liste
                first = plan[seen_codes[line["code"]]]
                step.update({"action": "acheter_meme_que", "reference": seen_codes[line["code"]], "nom": first["nom"]})
                entry.update({"statut": "meme_produit_que_ligne", "nom": first["nom"], "reference": first["ligne"]})
            else:
                status, detail = await self._import_resolve(client, products, line, known, found)
                if status == "ignore":
                    out.append({**entry, "statut": "ignore", "raison": detail})
                    continue
                step.update(detail)
                entry.update({"statut": status, "nom": detail["nom"]})
                if status == "connu" and detail.get("ajouter_code"):
                    entry["code_ajoute"] = True
                if status == "identifie":
                    entry["source"] = detail.get("source", "recherche multi-sources")
                if status == "a_creer_sous_ce_nom":
                    entry["a_verifier"] = True
                if line["code"]:
                    seen_codes[line["code"]] = len(plan)
            place = line["emplacement"]
            if place:
                match = self._by_name(locations, place)
                step["emplacement"] = {"id": int(match["id"])} if match else {"creer": place}
                entry["emplacement"] = clean_text(match["name"]) if match else f"{place} (à créer)"
                if not match:
                    new_places.setdefault(fold(place), place)
            if line["categorie"] and step["action"] in ("identifier_creer", "creer_nom"):
                match = self._by_name(groups, line["categorie"])
                step["categorie"] = {"id": int(match["id"])} if match else {"creer": line["categorie"]}
                entry["categorie"] = clean_text(match["name"]) if match else f"{line['categorie']} (à créer)"
                if not match:
                    new_groups.setdefault(fold(line["categorie"]), line["categorie"])
            step["ligne"] = number
            plan.append(step)
            out.append(entry)

        if not plan:
            return {"statut": "rien_a_importer", "message": "Aucune ligne importable.", "lignes": out}
        left = self._quota_left(caller)
        message = (
            "Aperçu de l'import : rien n'a été modifié. Montrez-le à la personne, puis rappelez importer_articles avec "
            "les mêmes articles et jeton_confirmation pour confirmer (5 minutes)."
        )
        if len(plan) > left:
            message += f" Attention : quota insuffisant ({left} actions restantes pour {len(plan)} achats), la confirmation sera refusée."
        ignored = [{"ligne": e["ligne"], "resultat": "ignore", "raison": e["raison"]} for e in out if e["statut"] == "ignore"]
        payload = {
            "empreinte": digest,
            "plan": plan,
            "ignores": ignored,
            "emplacements": list(new_places.values()),
            "categories": list(new_groups.values()),
        }
        return {
            "statut": "confirmation_requise",
            "message": message,
            "lignes": out,
            "a_creer": {"emplacements": payload["emplacements"], "categories": payload["categories"]},
            "jeton_confirmation": self._new_confirmation(caller, "importer_articles", payload),
        }

    async def _import_resolve(
        self, client: GrocyClient, products: list[dict], line: dict, known: dict, found: dict
    ) -> tuple[str, Any]:
        code, nom = line["code"], line["nom"]
        if code:
            if code in known:
                product = known[code]
                return "connu", {"action": "acheter", "product_id": int(product["id"]), "nom": clean_text(product["name"])}
            proposal = found.get(code)
            if proposal is _SLOW:
                return "ignore", "recherche multi-sources trop lente ou indisponible pour ce code : relancez l'aperçu plus tard"
            if proposal and proposal.get("name"):
                proposed = clean_text(proposal["name"], MAX_TEXT)
                same = self._by_name(products, proposed) or self._by_name(products, nom)
                if same:  # même produit déjà enregistré sous un autre code : on lui ajoute ce code
                    return "connu", {"action": "acheter", "product_id": int(same["id"]), "nom": clean_text(same["name"]), "ajouter_code": True}
                return "identifie", {
                    "action": "identifier_creer",
                    "nom": proposed,
                    "source": clean_text(proposal.get("description") or "recherche multi-sources", MAX_TEXT),
                    "creation": {
                        "brut_identique": proposed == proposal["name"],
                        "location_id": proposal.get("location_id"),
                        "qu_id": proposal.get("qu_id_stock"),
                        "description": clean_text(proposal.get("description") or "", 200),
                    },
                }
            if nom:
                same = self._by_name(products, nom)
                if same:
                    return "connu", {"action": "acheter", "product_id": int(same["id"]), "nom": clean_text(same["name"]), "ajouter_code": True}
                return "a_creer_sous_ce_nom", {"action": "creer_nom", "nom": nom, "creation": {}}
            return "ignore", "code-barres inconnu de Grocy et de la recherche multi-sources, et aucun nom fourni"
        status, match = await self._find_product(client, nom)
        if status == "ok":
            return "connu", {"action": "acheter", "product_id": int(match["id"]), "nom": clean_text(match["name"])}
        if status == "ambigu":
            names = ", ".join(clean_text(p["name"]) for p in match[:5])
            return "ignore", f"plusieurs produits correspondent ({names}) : précisez"
        return "ignore", "produit introuvable et sans code-barres : aucune création possible"

    async def _import_confirm(self, caller: Caller, payload: dict, slot: str) -> dict:
        plan, uid = payload["plan"], caller.grocy_user_id
        previous = self.store.reserve_idempotent(uid, slot, IDEMPOTENCY_TTL, _PENDING)
        if previous is not None:  # autre jeton de la même liste, déjà confirmé ou en cours
            return self._import_already(previous)
        state = {"transactions": [], "interrupted": True}

        def audit(event: str, **fields: object) -> None:
            self.audit.write(event, compte_id=uid, client=caller.client_id, **fields)

        async def run(client: GrocyClient) -> dict:
            locations = await client.locations()
            groups = await client.product_groups()
            units = await client.quantity_units()
            place_ids: dict[str, int] = {}
            for name in payload["emplacements"]:
                match = self._by_name(locations, name)
                if match:
                    place_ids[fold(name)] = int(match["id"])
                else:
                    place_ids[fold(name)] = await client.create_object("locations", {"name": name})
                    audit("creation", type="locations", id=place_ids[fold(name)], nom=name)
            group_ids: dict[str, int] = {}
            for name in payload["categories"]:
                match = self._by_name(groups, name)
                if match:
                    group_ids[fold(name)] = int(match["id"])
                else:
                    group_ids[fold(name)] = await client.create_object("product_groups", {"name": name})
                    audit("creation", type="product_groups", id=group_ids[fold(name)], nom=name)
            defaults = (int(locations[0]["id"]) if locations else None, int(units[0]["id"]) if units else None)

            def ref(value: dict | None, ids: dict[str, int]) -> int | None:
                if not value:
                    return None
                return int(value["id"]) if "id" in value else ids[fold(value["creer"])]

            results = []
            stop: str | None = None
            for step in plan:
                line = {"ligne": step["ligne"], "nom": step["nom"]}
                if stop:
                    results.append({**line, "resultat": "non_traite", "raison": stop})
                    continue
                try:
                    pid, created = await self._import_product(client, step, plan, ref, place_ids, group_ids, defaults, audit)
                    location_id = ref(step.get("emplacement"), place_ids)
                    rows = await client.purchase(pid, step["quantite"], best_before_date=step["date"], location_id=location_id)
                    transaction = str(rows[0]["transaction_id"]) if rows else None
                    if transaction:
                        state["transactions"].append(transaction)
                    results.append({**line, "resultat": "cree_et_ajoute" if created else "ajoute", "produit_id": pid})
                    audit("import_ligne", ligne=step["ligne"], resultat=results[-1]["resultat"], produit_id=pid, transaction=transaction)
                except _SkipLine as skip:
                    results.append({**line, "resultat": "ignore", "raison": str(skip)})
                    audit("import_ligne", ligne=step["ligne"], resultat="ignore", raison=str(skip))
                except (GrocyRequestError, GrocyForbidden) as error:
                    reason = f"refusé par Grocy : {clean_text(error.message, 200)}"
                    results.append({**line, "resultat": "ignore", "raison": reason})
                    audit("import_ligne", ligne=step["ligne"], resultat="ignore", raison=reason)
                except GrocyUnavailable:
                    stop = "Grocy ne répond plus : ligne non traitée"
                    results.append({**line, "resultat": "incertain", "raison": "Grocy n'a pas répondu ; vérifiez ce produit"})
                    audit("import_ligne", ligne=step["ligne"], resultat="incertain")
            done = sum(1 for r in results if r["resultat"] in ("ajoute", "cree_et_ajoute"))
            state["interrupted"] = stop is not None
            return {
                "statut": "ok" if done else "rien_importe",
                "message": f"{done} article(s) ajouté(s) au stock sur {len(plan)}."
                + (" annuler_derniere_action annule tous les achats de cet import." if done else ""),
                "lignes": sorted(results + payload.get("ignores", []), key=lambda r: r["ligne"]),
                "transaction_id": " ".join(state["transactions"]) or None,
            }

        result: dict | None = None
        try:
            result = await self._execute(caller, "importer_articles", {"lignes": len(plan), "confirmation": True}, run, count=len(plan))
        finally:
            # Toujours rendre annulables les achats déjà faits, même si le lot s'est interrompu (clé refusée, erreur…)
            transactions = state["transactions"]
            if transactions:
                self.store.record_undo(uid, " ".join(transactions), "importer_articles", f"import de {len(transactions)} article(s)")
            if result is not None and not state["interrupted"]:
                self.store.save_idempotent(uid, slot, json.dumps(result, ensure_ascii=False))
            elif transactions or result is not None:
                self.store.save_idempotent(uid, slot, _UNCERTAIN)
            else:
                self.store.release_idempotent(uid, slot)  # rien n'a été écrit (quota, panne avant tout achat)
        return result

    async def _import_product(self, client, step, plan, ref, place_ids, group_ids, defaults, audit) -> tuple[int, bool]:
        """Renvoie (id du produit, créé ?) pour une étape du plan ; crée la fiche si besoin."""
        if step["action"] == "acheter":
            pid = int(step["product_id"])
            if step.get("ajouter_code") and step.get("code") and not await client.product_by_barcode(step["code"]):
                await client.create_object("product_barcodes", {"product_id": pid, "barcode": step["code"]})
                audit("creation", type="product_barcodes", produit_id=pid, code=step["code"])
            step["_pid"] = pid
            return pid, False
        if step["action"] == "acheter_meme_que":
            first = plan[step["reference"]]
            if "_pid" not in first:
                raise _SkipLine("le produit de la ligne de référence n'a pas pu être créé")
            return int(first["_pid"]), False
        code = step["code"]
        if code and await client.product_by_barcode(code):  # apparu depuis l'aperçu : jamais de produit orphelin
            raise _SkipLine("ce code-barres a été ajouté à Grocy entre-temps : redemandez un aperçu")
        creation = step.get("creation") or {}
        group_id = ref(step.get("categorie"), group_ids)
        pid = None
        if step["action"] == "identifier_creer" and creation.get("brut_identique") and group_id is None:
            created = await client.external_lookup(code, add=True)
            pid = int(created["id"]) if created and "id" in created else None  # null : la source ne répond plus
        if pid is None:  # création avec la proposition gardée de l'aperçu (ou le nom fourni)
            default_place, default_unit = defaults
            location_id = ref(step.get("emplacement"), place_ids) or creation.get("location_id") or default_place
            unit = creation.get("qu_id") or default_unit
            data = {"name": step["nom"], "location_id": location_id, "qu_id_purchase": unit, "qu_id_stock": unit}
            data["description"] = creation.get("description") or "Créé par import (à vérifier)"
            if group_id is not None:
                data["product_group_id"] = group_id
            pid = await client.create_object("products", data)
            if code:
                await client.create_object("product_barcodes", {"product_id": pid, "barcode": code})
        audit("creation", type="products", id=pid, nom=step["nom"], code=code)
        step["_pid"] = pid
        return pid, True

    # --- apports nutritionnels et autonomie (spec B) --------------------------------------------------
    @staticmethod
    def _nutri(product: dict) -> dict:
        """Apports d'un produit lus dans ses champs personnalisés (valeurs texte de Grocy → nombres)."""
        fields = product.get("userfields") or {}
        values = {}
        for key, name in NUTRI_FIELDS.items():
            number = _to_number(fields.get(name))
            if number is not None:
                values[key] = number
        return {
            "valeurs": values,
            "poids": _to_number(fields.get(NUTRI_WEIGHT)),
            "source": clean_text(fields.get(NUTRI_SOURCE) or "", 200) or None,
            "non_alimentaire": str(fields.get(NUTRI_NON_FOOD) or "") in ("1", "true"),
        }

    async def _product_with_fields(self, client: GrocyClient, produit: str) -> tuple[str, Any]:
        status, found = await self._find_product(client, produit)
        if status != "ok":
            return status, found
        return "ok", {**found, "userfields": await client.userfields(int(found["id"]))}

    async def voir_nutrition(self, caller: Caller, produit: str) -> dict:
        async def run(client: GrocyClient) -> dict:
            status, product = await self._product_with_fields(client, produit)
            if status != "ok":
                return self._unresolved(status, product, produit)
            data, name = self._nutri(product), clean_text(product["name"])
            if data["non_alimentaire"]:
                return {"statut": "non_alimentaire", "produit": name, "message": f"{name} est marqué non alimentaire."}
            if not data["valeurs"]:
                return {
                    "statut": "sans_donnees",
                    "produit": name,
                    "message": f"Aucun apport nutritionnel pour {name}. completer_nutrition peut les chercher.",
                }
            out = {"statut": "ok", "produit": name, "pour_100g": data["valeurs"], "poids_unite_g": data["poids"], "source": data["source"]}
            if data["poids"]:
                out["par_unite"] = {k: round(v * data["poids"] / 100, 2) for k, v in data["valeurs"].items()}
            return out

        return await self._grocy(caller, run)

    async def _find_nutrition(self, client: GrocyClient, product: dict, codes: list[str], unit: str | None) -> dict:
        """Proposition pour un produit sans données : Open Food Facts puis USDA par code-barres, sinon Ciqual."""
        name = clean_text(product["name"])
        unavailable = False
        for code in codes:
            for fetch in (self.sources.off, self.sources.usda):
                try:
                    found = await fetch(code)
                except (SourceError, ValueError, TypeError, OverflowError, RecursionError):  # fiche piégée ou source en panne
                    unavailable = True
                    continue
                if not found or "kcal" not in found["valeurs"]:  # sans énergie, inutile pour l'autonomie
                    continue
                reason = check_values(found["valeurs"])
                if reason:
                    return {"produit": name, "statut": "refuse", "source": found["source"], "raison": reason}
                weight = found["poids"]
                per_unit = _UNIT_GRAMS.get(fold(unit or ""))
                if per_unit:  # stock compté en g, kg, ml, l… : le poids d'une unité est fixe, pas celui du paquet
                    weight = per_unit
                line = {
                    "produit": name,
                    "statut": "trouve",
                    "source": found["source"],
                    "reference": found["reference"],
                    "nom_source": found["nom"],
                    "valeurs": found["valeurs"],
                    "unite_stock": unit,
                    "poids_unite_g": weight[0] if weight else None,
                    "approximatif": bool(weight and weight[1]),
                }
                if weight is None:
                    line["message"] = "Poids d'une unité inconnu : à fixer avec definir_nutrition(poids_unite_g=…)."
                warnings = []
                pack = found.get("lot")
                if pack and not per_unit:
                    portion = _fmt(pack[1])
                    warnings.append(
                        f"Lot probable : {weight[0]} g = {pack[0]} × {portion} g. Le poids retenu est celui du paquet entier ; "
                        f"si le stock est compté à l'unité, demandez à la personne puis appelez "
                        f"definir_nutrition(poids_unite_g={pack[1]})."
                    )
                calories = _to_number(product.get("calories"))
                if calories:
                    warnings.append(f"L'énergie saisie dans Grocy ({calories} kcal par unité) sera remplacée.")
                if warnings:
                    line["avertissement"] = " ".join(warnings)
                return line
        candidates = [
            {"code": f["code"], "nom": f["nom"], "kcal": f.get("kcal")} for f in self.ciqual.search(name, limit=3)
        ]
        message = "Aucune donnée trouvée par code-barres."
        if unavailable:
            message = "Source de code-barres momentanément indisponible."
        if candidates:
            return {
                "produit": name,
                "statut": "choisir_ciqual",
                "candidats": candidates,
                "message": f"{message} Choisissez un aliment Ciqual avec la personne, puis appelez definir_nutrition "
                "avec son code (et poids_unite_g si possible).",
            }
        return {"produit": name, "statut": "introuvable", "message": f"{message} Rien dans Ciqual sous ce nom : cherchez avec rechercher_ciqual (mots simples), ou definir_nutrition(valeurs=…)."}

    async def completer_nutrition(
        self, caller: Caller, produits: list[str] | None = None, jeton_confirmation: str | None = None
    ) -> dict:
        tool = "completer_nutrition"
        if produits is not None and (not isinstance(produits, list) or not produits or len(produits) > MAX_IMPORT):
            raise ToolFailure(f"produits doit être une liste de 1 à {MAX_IMPORT} noms (ou absent : tous les produits).")
        names = [clean_text(p) for p in produits or []]
        digest = hashlib.sha256(json.dumps(names, ensure_ascii=False).encode()).hexdigest()
        if jeton_confirmation:
            payload = self._take_confirmation(caller, tool, jeton_confirmation)
            if payload.get("empreinte") != digest:
                raise ToolFailure("Liste différente de celle de l'aperçu : rien n'a été écrit. Redemandez un aperçu.")
            if len(payload["plan"]) > self._quota_left(caller):  # le jeton reste valable : réessayer plus tard
                self.store.save_confirmation(
                    sha256_hex(clean_text(jeton_confirmation, 100)), caller.grocy_user_id, tool,
                    json.dumps(payload), self.store.now() + CONFIRMATION_TTL,
                )
                raise ToolFailure(
                    f"Limite d'actions atteinte ({len(payload['plan'])} écritures) : rien n'a été écrit, le jeton reste "
                    "valable 5 minutes. Réessayez dans une minute."
                )
            return await self._nutrition_confirm(caller, payload["plan"])

        async def preview(client: GrocyClient) -> dict:
            barcodes: dict[int, list[str]] = {}
            for row in await client.product_barcodes():
                barcodes.setdefault(int(row["product_id"]), []).append(str(row["barcode"]))
            lines, targets = [], []
            if names:
                for name in names:
                    status, product = await self._product_with_fields(client, name)
                    if status != "ok":
                        lines.append({"produit": name, **self._unresolved(status, product, name)})
                    else:
                        targets.append(product)
            else:
                all_products = [p for p in await client.products() if int(p.get("active", 1)) == 1]
                pending = [p for p in all_products if not self._nutri(p)["valeurs"] and not self._nutri(p)["non_alimentaire"]]
                targets = pending[:MAX_IMPORT]
                if len(pending) > MAX_IMPORT:
                    lines.append({"statut": "suite", "message": f"{len(pending) - MAX_IMPORT} autres produits : rappelez ensuite."})
            units = {int(u["id"]): str(u.get("name") or "") for u in await client.quantity_units()}
            plan, todo = [], []
            for product in targets:
                data = self._nutri(product)
                if data["valeurs"] or data["non_alimentaire"]:
                    lines.append({"produit": clean_text(product["name"]), "statut": "deja_renseigne",
                                  "message": "Déjà renseigné : utilisez definir_nutrition pour modifier."})
                else:
                    todo.append(product)
            found: dict[int, dict] = {}
            limit = anyio.Semaphore(5)

            async def one(product: dict) -> None:
                async with limit:
                    unit = units.get(int(product.get("qu_id_stock") or 0))
                    try:
                        with anyio.fail_after(PRODUCT_LOOKUP_SECONDS):
                            found[int(product["id"])] = await self._find_nutrition(
                                client, product, barcodes.get(int(product["id"]), []), unit
                            )
                    except TimeoutError:
                        found[int(product["id"])] = {"produit": clean_text(product["name"]), "statut": "introuvable",
                                                     "message": "Sources trop lentes pour ce produit : relancez plus tard."}

            async with anyio.create_task_group() as group:
                for product in todo:
                    group.start_soon(one, product)
            for product in todo:
                line = found[int(product["id"])]
                lines.append(line)
                if line["statut"] == "trouve":
                    plan.append({**line, "product_id": int(product["id"])})
            if not plan:
                return {"statut": "rien_a_ecrire", "lignes": lines}
            payload = {"empreinte": digest, "plan": plan}
            return {
                "statut": "confirmation_requise",
                "message": "Aperçu : rien n'a été écrit. Montrez-le à la personne, puis rappelez completer_nutrition avec "
                "les mêmes produits et jeton_confirmation (5 minutes).",
                "lignes": lines,
                "jeton_confirmation": self._new_confirmation(caller, tool, payload),
            }

        return await self._grocy(caller, preview)

    async def _write_nutrition(
        self, client: GrocyClient, product_id: int, values: dict, weight: float | None, source: str | None, current: dict
    ) -> dict:
        fields: dict[str, object] = {NUTRI_FIELDS[k]: _to_number(v) for k, v in values.items()}  # 145.0 → 145
        if weight is not None:
            fields[NUTRI_WEIGHT] = _to_number(weight)
        if source is not None:
            fields[NUTRI_SOURCE] = source
        await client.set_userfields(product_id, fields)
        kcal = values.get("kcal", current["valeurs"].get("kcal"))
        grams = weight if weight is not None else current["poids"]
        if kcal is not None and grams:
            await client.set_product_calories(product_id, kcal * grams / 100)
        return fields

    async def _nutrition_confirm(self, caller: Caller, plan: list[dict]) -> dict:
        async def run(client: GrocyClient) -> dict:
            results = []
            for step in plan:
                current = self._nutri({"userfields": await client.userfields(step["product_id"])})
                if current["valeurs"] or current["non_alimentaire"]:
                    results.append({"produit": step["produit"], "resultat": "ignore", "raison": "renseigné entre-temps"})
                    continue
                source = f"{step['source']} {step['reference']}" + (" (ml≈g)" if step.get("approximatif") else "")
                await self._write_nutrition(client, step["product_id"], step["valeurs"], step["poids_unite_g"], source, current)
                self.audit.write("nutrition", compte_id=caller.grocy_user_id, produit_id=step["product_id"], source=source, valeurs=step["valeurs"])
                results.append({"produit": step["produit"], "resultat": "ecrit", "poids_connu": step["poids_unite_g"] is not None})
            done = sum(r["resultat"] == "ecrit" for r in results)
            return {"statut": "ok" if done else "rien_ecrit", "message": f"Apports écrits pour {done} produit(s).", "lignes": results}

        return await self._execute(caller, "completer_nutrition", {"produits": len(plan), "confirmation": True}, run, count=len(plan))

    async def definir_nutrition(
        self,
        caller: Caller,
        produit: str,
        ciqual: int | None = None,
        valeurs: dict | None = None,
        poids_unite_g: float | None = None,
        non_alimentaire: bool | None = None,
        cle_idempotence: str | None = None,
    ) -> dict:
        if ciqual is None and valeurs is None and poids_unite_g is None and non_alimentaire is None:
            raise ToolFailure("Indiquez ciqual, valeurs, poids_unite_g ou non_alimentaire.")
        new_values: dict = {}
        source = None
        if ciqual is not None:
            food = self.ciqual.get(ciqual) if isinstance(ciqual, int) and not isinstance(ciqual, bool) else None
            if food is None:
                raise ToolFailure("Code Ciqual inconnu : reprenez un code proposé par completer_nutrition.")
            if "kcal" not in food:
                raise ToolFailure("Cet aliment Ciqual n'a pas de valeur d'énergie : choisissez-en un autre.")
            new_values = {k: food[k] for k in NUTRI_FIELDS if k in food}
            source = clean_text(f"Ciqual {food['code']} — {food['nom']}", 200)
        if valeurs is not None:
            if not isinstance(valeurs, dict) or not valeurs or set(valeurs) - set(NUTRI_FIELDS):
                raise ToolFailure(f"valeurs : clés possibles {', '.join(NUTRI_FIELDS)} (pour 100 g).")
            new_values.update(valeurs)
            source = "saisie manuelle" if ciqual is None else source
        reason = check_values(new_values)
        if reason:
            raise ToolFailure(f"Valeurs refusées : {reason}.")
        if poids_unite_g is not None and (not _is_number(poids_unite_g) or not 1 <= poids_unite_g <= 50_000):  # type: ignore[operator]
            raise ToolFailure("poids_unite_g doit être compris entre 1 et 50 000 g.")
        args = {"produit": clean_text(produit), "ciqual": ciqual, "valeurs": new_values or None,
                "poids_unite_g": poids_unite_g, "non_alimentaire": non_alimentaire}

        async def run(client: GrocyClient) -> dict:
            status, product = await self._product_with_fields(client, produit)
            if status != "ok":
                return self._unresolved(status, product, produit)
            product_id, name, current = int(product["id"]), clean_text(product["name"]), self._nutri(product)
            if non_alimentaire is not None:
                await client.set_userfields(product_id, {NUTRI_NON_FOOD: "1" if non_alimentaire else "0"})
            if new_values or poids_unite_g is not None:
                merged = {**current["valeurs"], **new_values}
                reason = check_values(merged)
                if reason:
                    raise ToolFailure(f"Valeurs refusées une fois combinées aux existantes : {reason}.")
                await self._write_nutrition(client, product_id, new_values, poids_unite_g, source, current)
            self.audit.write("nutrition", compte_id=caller.grocy_user_id, produit_id=product_id, source=source, valeurs=new_values)
            return {"statut": "ok", "produit": name, "message": f"Apports de {name} mis à jour."}

        return await self._action(caller, "definir_nutrition", args, cle_idempotence, run)

    # --- conditionnement (spec conditionnement C8) -----------------------------------------------------
    _PACKAGING_HELP = {
        "a_ecrire": "contenance certaine : réglé automatiquement au prochain passage (toutes les 10 minutes)",
        "lot": "lot de plusieurs éléments : précisez avec definir_conditionnement (ex. 1 Pack = 6 Bouteille, "
        "puis 1 Bouteille = 27,5 cL)",
        "contradictoire": "contenances discordantes (nom, Open Food Facts) : précisez avec definir_conditionnement",
        "incompatible": "contenance en volume pour un produit au poids (ou l'inverse) : précisez avec definir_conditionnement",
        "inconnu": "contenance introuvable : précisez avec definir_conditionnement (ex. 1 Paquet = 250 g)",
        "unite_absente": "unité de contenance absente de Grocy : créez-la, ou précisez avec definir_conditionnement",
        "unite_stock": "l'unité de stock n'est pas un emballage (tranche, portion…) : précisez avec definir_conditionnement",
        "reporte": "Open Food Facts pas encore consulté : examiné au prochain passage automatique",
        "retire": "contenance retirée ou écartée à la main : le passage automatique n'y touche plus "
        "(definir_conditionnement reste possible)",
    }
    MAX_PACKAGING_LOOKUPS = 20  # appels à Open Food Facts par appel de l'outil (délai de réponse borné)

    async def voir_conditionnements_manquants(self, caller: Caller) -> dict:
        """Produits sans conversion propre, avec ce que fera le passage automatique ou ce qu'il faut préciser."""

        async def run(client: GrocyClient) -> dict:
            products, units, conversions, codes = await load_catalog(client)
            done = JobState(self.settings.packaging_done_path).done if self.settings.packaging_done_path else set()
            lines, lookups = [], 0
            for product in products:
                pid = int(product["id"])
                own = codes.get(pid, [])
                if pid in done and not own_conversions(pid, conversions):
                    lines.append({"produit": clean_text(product["name"]), "id": pid, "statut": "retire",
                                  "explication": self._PACKAGING_HELP["retire"]})
                    continue
                if needs_lookup(product, conversions, own, self._off_cache):
                    if lookups >= self.MAX_PACKAGING_LOOKUPS:  # jamais décidé sans Open Food Facts (revue M2)
                        lines.append({"produit": clean_text(product["name"]), "id": pid, "statut": "reporte",
                                      "explication": self._PACKAGING_HELP["reporte"]})
                        continue
                    lookups += 1
                decision = await decide(product, units, conversions, own, self.sources, self._off_cache)
                if decision.status == "deja":
                    continue
                lines.append({
                    "produit": clean_text(product["name"]),
                    "id": pid,
                    "unite_stock": clean_text(units.get(int(product["qu_id_stock"]), "")),
                    "statut": decision.status,
                    "contenance": describe(decision.content),
                    "source": decision.source,
                    "explication": self._PACKAGING_HELP[decision.status],
                })
            return {
                "produits": lines[:MAX_RESULTS * 10],
                "message": f"{len(lines)} produit(s) sans conditionnement." if lines else "Tous les produits ont un conditionnement.",
            }

        return await self._grocy(caller, run)

    async def definir_conditionnement(
        self,
        caller: Caller,
        produit: str,
        quantite: float,
        unite: str,
        par: str | None = None,
        cle_idempotence: str | None = None,
    ) -> dict:
        if not _is_number(quantite) or not 0 < quantite <= 1_000_000:  # type: ignore[operator]
            raise ToolFailure("quantite doit être un nombre strictement positif, au plus 1 000 000.")
        if not isinstance(unite, str) or not unite.strip() or (par is not None and not isinstance(par, str)):
            raise ToolFailure("Indiquez l'unité de contenance (ex. « g », « cL », « Bouteille »).")
        args = {"produit": clean_text(produit), "quantite": quantite, "unite": clean_text(unite, 40),
                "par": clean_text(par, 40) if par else None}

        async def run(client: GrocyClient) -> dict:
            status, product = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, product, produit)
            rows = await client.quantity_units()
            units = {int(u["id"]): str(u["name"]) for u in rows}
            plurals = {int(u["id"]): str(u.get("name_plural") or "") for u in rows}
            target = resolve_unit(units, unite, plurals)
            source = resolve_unit(units, par, plurals) if par else int(product["qu_id_stock"])
            for wanted, found in ((unite, target), (par, source)):
                if found is None:
                    names = ", ".join(clean_text(n) for n in units.values())
                    raise ToolFailure(f"Unité « {clean_text(wanted, 40)} » inconnue de Grocy. Unités : {names}.")
            assert target is not None and source is not None
            if target == source:
                raise ToolFailure("Les deux unités doivent être différentes (ex. 1 Paquet = 250 g).")
            source_kind, target_kind = metric_kind(units[source]), metric_kind(units[target])
            if source_kind and target_kind:  # « 1 kg = 250 g » propre au produit fausserait le stock (revue I3)
                raise ToolFailure(
                    "Les deux unités sont des unités de poids ou volume : la conversion existe déjà dans Grocy. "
                    "Indiquez l'emballage dans « par » (ex. par=\"Paquet\", quantite=250, unite=\"g\")."
                )
            pid, name = int(product["id"]), clean_text(product["name"])
            label = f"1 {clean_text(units[source])} = {float(quantite):g} {clean_text(units[target])}"
            mine = own_conversions(pid, await client.quantity_unit_conversions() or [])
            for row in mine:
                a, b, factor = int(row["from_qu_id"]), int(row["to_qu_id"]), float(row["factor"])
                if (a, b) == (source, target) and abs(factor - float(quantite)) <= 1e-9 * max(1.0, float(quantite)):
                    return {"statut": "existe_deja", "produit": name, "message": f"{name} : {label} existe déjà, rien n'a changé."}
                # même paire (dans un sens ou l'autre) ou même dimension déjà réglée pour cette unité (revue I4)
                for here, other in ((a, b), (b, a)):
                    other_kind = metric_kind(units.get(other, ""))
                    clash = (here == source and (other == target or (target_kind and other_kind and other_kind[0] == target_kind[0]))) or (
                        here == target and (other == source or (source_kind and other_kind and other_kind[0] == source_kind[0])))
                    if clash:
                        raise ToolFailure(
                            f"Une conversion existe déjà pour {name} : 1 {clean_text(units.get(a, '?'))} = {factor:g} "
                            f"{clean_text(units.get(b, '?'))}. Corrigez-la dans Grocy (fiche du produit, conversions d'unités)."
                        )
            await client.create_object(
                "quantity_unit_conversions", {"product_id": pid, "from_qu_id": source, "to_qu_id": target, "factor": float(quantite)}
            )
            self.audit.write("conditionnement", compte_id=caller.grocy_user_id, produit_id=pid, source="manuel",
                             conversions=[[source, target, float(quantite)]])
            return {"statut": "ok", "produit": name, "message": f"Conditionnement de {name} : {label}."}

        return await self._action(caller, "definir_conditionnement", args, cle_idempotence, run)

    async def rechercher_ciqual(self, caller: Caller, texte: str) -> dict:
        """Recherche libre dans la table Ciqual (aucun appel à Grocy) : pour choisir le code de definir_nutrition."""
        text = clean_text(texte)
        if not text.strip():
            raise ToolFailure("Indiquez un aliment à chercher, en mots simples (par exemple « café soluble »).")
        foods = self.ciqual.search(text, limit=8)
        return {
            "candidats": [
                {"code": f["code"], "nom": f["nom"], "kcal": f.get("kcal"), "proteines": f.get("proteines")} for f in foods
            ],
            "message": "Choisissez avec la personne, puis appelez definir_nutrition(produit, ciqual=code)."
            if foods
            else "Aucun aliment Ciqual : essayez d'autres mots, plus génériques.",
        }

    async def autonomie_alimentaire(self, caller: Caller, inclure_perimes: bool = False) -> dict:
        if not self.settings.household:
            return {
                "statut": "foyer_non_configure",
                "message": "Le foyer n'est pas configuré sur le serveur (MCP_HOUSEHOLD) : calcul impossible.",
            }
        needs = daily_needs(list(self.settings.household))

        async def run(client: GrocyClient) -> dict:
            today = datetime.fromtimestamp(self.store.now(), ZoneInfo(self.settings.timezone)).date().isoformat()
            products = {int(p["id"]): p for p in await client.products()}
            totals = {"kcal": 0.0, "proteines": 0.0}
            covered, missing, expired = set(), {}, 0
            for lot in await client.stock_entries():
                product = products.get(int(lot["product_id"]))
                amount = _to_number(lot.get("amount")) or 0
                if product is None or amount <= 0:
                    continue
                data = self._nutri(product)
                if data["non_alimentaire"]:
                    continue
                if not inclure_perimes and str(lot.get("best_before_date") or "9999") < today:
                    expired += 1
                    continue
                weight = data["poids"]
                valid = "kcal" in data["valeurs"] and weight and 1 <= weight <= 50_000 and not check_values(data["valeurs"])
                if not valid:  # absentes, ou saisies à la main de façon invraisemblable
                    missing[int(product["id"])] = clean_text(product["name"])
                    continue
                covered.add(int(product["id"]))
                grams = amount * data["poids"]
                totals["kcal"] += grams * data["valeurs"]["kcal"] / 100
                totals["proteines"] += grams * data["valeurs"].get("proteines", 0) / 100
            food = len(covered) + len(missing)
            coverage = round(100 * len(covered) / food) if food else 0
            days = {"energie": round(totals["kcal"] / needs["kcal"], 1), "proteines": round(totals["proteines"] / needs["proteines"], 1)}
            out = {
                "statut": "ok",
                "foyer": [f"{'homme' if s == 'H' else 'femme'} {a} ans" for s, a in self.settings.household],
                "besoins_par_jour": needs,
                "total": {k: round(v, 1) for k, v in totals.items()},
                "jours": days,
                "facteur_limitant": "énergie" if days["energie"] <= days["proteines"] else "protéines",
                "couverture_pct": coverage,
                "sans_donnees": [{"id": pid, "nom": name} for pid, name in sorted(missing.items())][:MAX_RESULTS],
                "perimes_exclus": expired,
                "message": f"Environ {min(days.values())} jour(s) pour le foyer, d'après les besoins de référence "
                "(EFSA, activité modérée).",
            }
            if coverage < 80:
                out["avertissement"] = (
                    f"Seuls {coverage} % des produits alimentaires en stock ont des apports connus : le résultat "
                    "sous-estime l'autonomie. completer_nutrition peut compléter les autres."
                )
            return out

        return await self._grocy(caller, run)

class _SkipLine(Exception):
    """Ligne d'import ignorée à la confirmation (raison destinée à la personne)."""
