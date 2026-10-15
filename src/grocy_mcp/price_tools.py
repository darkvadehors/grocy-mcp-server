"""Outils de prix des courses (spec prix P1 à P5) : ticket, prix au rayon, comparaison, liste répartie, dépenses.

Grocy reste la référence (achats avec prix et magasin) ; seuls les prix vus en rayon, la mémoire des libellés de
ticket et les empreintes de tickets vivent dans la base du serveur. Open Prices n'est lu qu'en dernier recours.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from grocy_mcp.grocy import GrocyClient
from grocy_mcp.prices import latest_by_store, match_store, normalized_price, stock_amount
from grocy_mcp.sources import SourceError
from grocy_mcp.tools import Caller, ToolFailure, ToolService, _product_ref, clean_text

MAX_LINE_PRICE = 1000
OLD_DAYS = 90
STORE_NAME = 60


def _is_amount(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _store_name(row: dict | None) -> str:
    return clean_text((row or {}).get("name"), STORE_NAME)


class PricesService(ToolService):
    # --- aides communes ------------------------------------------------------------------------------
    def _today(self) -> date:
        return datetime.fromtimestamp(self.store.now(), ZoneInfo(self.settings.timezone)).date()

    @staticmethod
    async def _units_and_conversions(client: GrocyClient) -> tuple[dict[int, str], list[dict]]:
        units = {int(u["id"]): str(u.get("name", "")) for u in await client.quantity_units()}
        return units, await client.quantity_unit_conversions()

    async def _store_for(self, caller: Caller, client: GrocyClient, name: object) -> tuple[dict, bool]:
        """Magasin Grocy du nom donné ; créé s'il n'existe pas (renvoie aussi « créé ? »)."""
        text = self._master_name(name)
        shops = await client.shopping_locations()
        found = match_store(shops, text)
        if found:
            return found, False
        new_id = await client.create_object("shopping_locations", {"name": text})
        self.audit.write("creation", compte_id=caller.grocy_user_id, type="shopping_locations", id=new_id, nom=text)
        return {"id": new_id, "name": text}, True

    @staticmethod
    def _line_price(value: object, name: str = "prix") -> float:
        if not _is_amount(value) or not 0 < value <= MAX_LINE_PRICE:  # type: ignore[operator]
            raise ToolFailure(f"{name} doit être un nombre strictement positif, au plus {MAX_LINE_PRICE} €.")
        return float(value)  # type: ignore[arg-type]

    # --- noter_prix (P2) -----------------------------------------------------------------------------
    async def noter_prix(
        self,
        caller: Caller,
        produit: str,
        magasin: str,
        prix: float,
        quantite: float | None = None,
        poids_kg: float | None = None,
        cle_idempotence: str | None = None,
    ) -> dict:
        price = self._line_price(prix)
        if quantite is not None:
            quantite = self._quantity(quantite)
        if poids_kg is not None and (not _is_amount(poids_kg) or not 0.001 <= poids_kg <= 50):
            raise ToolFailure("poids_kg doit être compris entre 0,001 et 50.")
        tool = "noter_prix"

        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            units, conversions = await self._units_and_conversions(client)
            amount = stock_amount({"quantite": quantite, "poids_kg": poids_kg}, found, units, conversions)
            if not amount:
                raise ToolFailure(
                    "Impossible de convertir ce poids ou cette quantité dans l'unité de stock du produit : définissez "
                    "d'abord son conditionnement (definir_conditionnement), ou notez le prix d'une unité."
                )
            shop, created = await self._store_for(caller, client, magasin)
            per_unit = round(price / amount, 4)
            day = self._today().isoformat()
            price_id = self.store.add_price(
                caller.grocy_user_id, product_id=int(found["id"]), location_id=int(shop["id"]), price=per_unit, day=day,
                source="rayon",
            )
            summary = f"prix de {clean_text(found['name'])} chez {_store_name(shop)} noté"
            self.store.record_undo(caller.grocy_user_id, f"prix:{price_id}", tool, summary)
            return {
                "statut": "ok",
                "message": summary[:1].upper() + summary[1:] + ". annuler_derniere_action le retire.",
                "produit": _product_ref(found),
                "magasin": _store_name(shop),
                "magasin_cree": created,
                "prix_unite_stock": per_unit,
                "prix_normalise": normalized_price(per_unit, found, units, conversions),
            }

        args = {"produit": clean_text(produit), "magasin": clean_text(magasin), "prix": price, "quantite": quantite,
                "poids_kg": poids_kg}
        return await self._action(caller, tool, args, cle_idempotence, run)

    # --- comparer_prix (P3) --------------------------------------------------------------------------
    async def _known_prices(self, client: GrocyClient, product_id: int) -> list[dict]:
        """Prix d'achat Grocy et prix notés (rayon, ticket) d'un produit, par unité de stock."""
        rows = [
            {
                "location_id": (h.get("shopping_location") or {}).get("id"),
                "prix": float(h["price"]),
                "date": str(h.get("date") or "")[:10],
                "source": "achat",
            }
            for h in await client.price_history(product_id)
            if _is_amount(_to_float(h.get("price"))) and float(h["price"]) > 0
        ]
        return rows + self.store.prices_for([product_id])

    async def comparer_prix(self, caller: Caller, produit: str) -> dict:
        async def run(client: GrocyClient) -> dict:
            status, found = await self._find_product(client, produit)
            if status != "ok":
                return self._unresolved(status, found, produit)
            pid = int(found["id"])
            units, conversions = await self._units_and_conversions(client)
            shops = {int(s["id"]): s for s in await client.shopping_locations()}
            old = (self._today() - timedelta(days=OLD_DAYS)).isoformat()
            stores = []
            for loc, row in latest_by_store(await self._known_prices(client, pid)).items():
                stores.append({
                    "magasin": _store_name(shops.get(loc)) or f"magasin {loc}",
                    "prix_unite_stock": round(row["prix"], 4),
                    "prix_normalise": normalized_price(row["prix"], found, units, conversions),
                    "date": row["date"],
                    "source": row["source"],
                    "ancien": row["date"] < old,
                })
            stores.sort(key=lambda s: (s["prix_normalise"]["valeur"], s["magasin"]))
            reference = None if stores else await self._france_reference(client, produit, pid)
            return {
                "statut": "ok",
                "produit": _product_ref(found),
                "magasins": stores,
                "moins_cher": stores[0]["magasin"] if stores else None,
                "reference_france": reference,
                "message": "Prix par magasin, le moins cher en premier ; « ancien » : relevé il y a plus de 90 jours."
                if stores else "Aucun prix connu dans vos magasins : notez-en un (noter_prix) ou importez un ticket.",
            }

        return await self._grocy(caller, run)

    async def _france_reference(self, client: GrocyClient, produit: str, product_id: int) -> dict | None:
        text = clean_text(produit)
        codes = [text] if text.isdigit() else [
            str(b["barcode"]) for b in await client.product_barcodes() if int(b.get("product_id", -1)) == product_id
        ]
        code = next((c for c in codes if 8 <= len(c) <= 14 and c.isdigit()), None)
        if code is None:
            return None
        try:
            found = await self.sources.open_prices(code, today=self._today().isoformat())
        except SourceError:
            return {"indisponible": True}
        if not found:
            return {"prix": [], "avertissement": "Aucun prix récent sur Open Prices."}
        values = [p["prix"] for p in found]
        return {
            "prix": found,
            "mediane": round(statistics.median(values), 2),
            "min": min(values),
            "max": max(values),
            "avertissement": "Prix relevés ailleurs en France (Open Prices), à titre indicatif, pour le produit entier.",
        }


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
