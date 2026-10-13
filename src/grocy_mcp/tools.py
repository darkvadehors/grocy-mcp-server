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
            for transaction in reversed(pending):
                try:
                    await client.undo(transaction)
                except GrocyRequestError as error:
                    raise ToolFailure(
                        f"Annulation impossible : le stock a changé depuis cette action ({clean_text(error.message, 200)})."
                    ) from None
            self.store.mark_undone(last.rowid)
            note = ""
            if last.tool == "importer_articles":
                note = " Les produits, emplacements et catégories créés par l'import restent dans Grocy."
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
        place = clean_text(raw.get("emplacement"), MAX_NAME) if raw.get("emplacement") not in (None, "") else None
        group = clean_text(raw.get("categorie"), MAX_NAME) if raw.get("categorie") not in (None, "") else None
        return {"code": code, "nom": nom, "quantite": _num(quantite), "date": when, "emplacement": place, "categorie": group}

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
        canonical = json.dumps(lines, sort_keys=True, ensure_ascii=False)
        slot = f"{tool}:{hashlib.sha256(canonical.encode()).hexdigest()}"
        if jeton_confirmation:
            return await self._import_confirm(caller, self._take_confirmation(caller, tool, jeton_confirmation), slot)
        previous = self.store.get_idempotent(caller.grocy_user_id, slot, IDEMPOTENCY_TTL)
        if previous is not None:
            return {
                "statut": "deja_importe",
                "message": "Cette même liste a déjà été importée il y a moins de 10 minutes : rien n'a été refait. "
                "Si c'est voulu (nouvel achat identique), attendez ou modifiez la liste.",
                "bilan": json.loads(previous).get("lignes", []),
            }
        return await self._grocy(caller, lambda client: self._import_preview(caller, client, lines))

    async def _import_preview(self, caller: Caller, client: GrocyClient, lines: list[dict]) -> dict:
        products = [p for p in await client.products() if int(p.get("active", 1)) == 1]
        locations, groups = await client.locations(), await client.product_groups()
        out, plan, new_places, new_groups = [], [], {}, {}
        seen_codes: dict[str, int] = {}
        for number, line in enumerate(lines, start=1):
            if "erreur" in line:
                out.append({"ligne": number, "statut": "ignore", "raison": line["erreur"]})
                continue
            entry = {"ligne": number, "quantite": line["quantite"], "date_peremption": line["date"] or NEVER}
            step: dict = {"quantite": line["quantite"], "date": line["date"] or NEVER, "code": line["code"]}
            if line["code"] and line["code"] in seen_codes:  # même code plus haut dans la liste
                first = plan[seen_codes[line["code"]]]
                step.update({k: first[k] for k in ("action", "product_id", "nom", "creation") if k in first})
                step["action"] = "acheter_meme_que" if first["action"] != "acheter" else "acheter"
                step["reference"] = seen_codes[line["code"]]
                entry.update({"statut": "meme_produit_que_ligne", "nom": first["nom"], "reference": first["ligne"]})
            else:
                status, detail = await self._import_resolve(client, products, line)
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
                found = self._by_name(locations, place)
                step["emplacement"] = {"id": int(found["id"])} if found else {"creer": place}
                entry["emplacement"] = clean_text(found["name"]) if found else f"{place} (à créer)"
                if not found:
                    new_places.setdefault(fold(place), place)
            if line["categorie"] and step["action"] in ("identifier_creer", "creer_nom"):
                found = self._by_name(groups, line["categorie"])
                step["categorie"] = {"id": int(found["id"])} if found else {"creer": line["categorie"]}
                entry["categorie"] = clean_text(found["name"]) if found else f"{line['categorie']} (à créer)"
                if not found:
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

    async def _import_resolve(self, client: GrocyClient, products: list[dict], line: dict) -> tuple[str, Any]:
        code, nom = line["code"], line["nom"]
        if code:
            details = await client.product_by_barcode(code)
            if details:
                product = details["product"]
                return "connu", {"action": "acheter", "product_id": int(product["id"]), "nom": clean_text(product["name"])}
            proposal = await client.external_lookup(code)
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
        status, found = await self._find_product(client, nom)
        if status == "ok":
            return "connu", {"action": "acheter", "product_id": int(found["id"]), "nom": clean_text(found["name"])}
        if status == "ambigu":
            names = ", ".join(clean_text(p["name"]) for p in found[:5])
            return "ignore", f"plusieurs produits correspondent ({names}) : précisez"
        return "ignore", "produit introuvable et sans code-barres : aucune création possible"

    async def _import_confirm(self, caller: Caller, payload: dict, slot: str) -> dict:
        plan = payload["plan"]

        async def run(client: GrocyClient) -> dict:
            locations = await client.locations()
            groups = await client.product_groups()
            units = await client.quantity_units()
            place_ids: dict[str, int] = {}
            for name in payload["emplacements"]:
                found = self._by_name(locations, name)
                place_ids[fold(name)] = int(found["id"]) if found else await client.create_object("locations", {"name": name})
            group_ids: dict[str, int] = {}
            for name in payload["categories"]:
                found = self._by_name(groups, name)
                group_ids[fold(name)] = int(found["id"]) if found else await client.create_object("product_groups", {"name": name})
            default_place = int(locations[0]["id"]) if locations else None
            default_unit = int(units[0]["id"]) if units else None

            def ref(value: dict | None, ids: dict[str, int]) -> int | None:
                if not value:
                    return None
                return int(value["id"]) if "id" in value else ids[fold(value["creer"])]

            results, transactions = [], []
            stop: str | None = None
            for step in plan:
                line = {"ligne": step["ligne"], "nom": step["nom"]}
                if stop:
                    results.append({**line, "resultat": "non_traite", "raison": stop})
                    continue
                try:
                    pid, created = await self._import_product(client, step, plan, ref, place_ids, group_ids, default_place, default_unit)
                    location_id = ref(step.get("emplacement"), place_ids)
                    rows = await client.purchase(pid, step["quantite"], best_before_date=step["date"], location_id=location_id)
                    if rows:
                        transactions.append(str(rows[0]["transaction_id"]))
                    results.append({**line, "resultat": "cree_et_ajoute" if created else "ajoute", "produit_id": pid})
                except _SkipLine as skip:
                    results.append({**line, "resultat": "ignore", "raison": str(skip)})
                except (GrocyRequestError, GrocyForbidden) as error:
                    results.append({**line, "resultat": "ignore", "raison": f"refusé par Grocy : {clean_text(error.message, 200)}"})
                except GrocyUnavailable:
                    stop = "Grocy ne répond plus : ligne non traitée"
                    results.append({**line, "resultat": "incertain", "raison": "Grocy n'a pas répondu ; vérifiez ce produit"})
            done = sum(1 for r in results if r["resultat"] in ("ajoute", "cree_et_ajoute"))
            if transactions:
                self.store.record_undo(caller.grocy_user_id, " ".join(transactions), "importer_articles", f"import de {done} article(s)")
            all_lines = sorted(results + payload.get("ignores", []), key=lambda r: r["ligne"])
            return {
                "statut": "ok" if done else "rien_importe",
                "message": f"{done} article(s) ajouté(s) au stock sur {len(plan)}."
                + (" annuler_derniere_action annule tous les achats de cet import." if done else ""),
                "lignes": all_lines,
                "transaction_id": " ".join(transactions) or None,
            }

        result = await self._execute(caller, "importer_articles", {"lignes": len(plan), "confirmation": True}, run, count=len(plan))
        if result.get("statut") == "ok":
            self.store.save_idempotent(caller.grocy_user_id, slot, json.dumps(result, ensure_ascii=False))
        return result

    async def _import_product(self, client, step, plan, ref, place_ids, group_ids, default_place, default_unit) -> tuple[int, bool]:
        """Renvoie (id du produit, créé ?) pour une étape du plan ; crée la fiche si besoin."""
        if step["action"] == "acheter":
            pid = int(step["product_id"]) if "product_id" in step else int(plan[step["reference"]]["_pid"])
            if step.get("ajouter_code") and step.get("code") and not await client.product_by_barcode(step["code"]):
                await client.create_object("product_barcodes", {"product_id": pid, "barcode": step["code"]})
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
        if step["action"] == "identifier_creer" and creation.get("brut_identique") and group_id is None:
            created = await client.external_lookup(code, add=True)
            if not created or "id" not in created:
                raise _SkipLine("la recherche multi-sources ne propose plus ce produit")
            pid = int(created["id"])
        else:
            location_id = ref(step.get("emplacement"), place_ids) or creation.get("location_id") or default_place
            unit = creation.get("qu_id") or default_unit
            data = {"name": step["nom"], "location_id": location_id, "qu_id_purchase": unit, "qu_id_stock": unit}
            data["description"] = creation.get("description") or "Créé par import (à vérifier)"
            if group_id is not None:
                data["product_group_id"] = group_id
            pid = await client.create_object("products", data)
            if code:
                await client.create_object("product_barcodes", {"product_id": pid, "barcode": code})
        self.audit.write("creation", type="products", id=pid, nom=step["nom"])
        step["_pid"] = pid
        return pid, True


class _SkipLine(Exception):
    """Ligne d'import ignorée à la confirmation (raison destinée à la personne)."""
