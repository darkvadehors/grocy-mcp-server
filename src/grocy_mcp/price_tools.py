"""Outils de prix des courses (spec prix P1 à P5) : ticket, prix au rayon, comparaison, liste répartie, dépenses.

Grocy reste la référence (achats avec prix et magasin) ; seuls les prix vus en rayon, la mémoire des libellés de
ticket et les empreintes de tickets vivent dans la base du serveur. Open Prices n'est lu qu'en dernier recours.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from grocy_mcp.grocy import GrocyClient, GrocyForbidden, GrocyRequestError, GrocyUnavailable
from grocy_mcp.prices import (
    apply_discounts,
    best_split,
    check_total,
    fold,
    latest_by_store,
    match_store,
    normalized_price,
    price_rises,
    stock_amount,
    suggest_products,
)
from grocy_mcp.packaging import metric_kind
from grocy_mcp.sources import SourceError
from grocy_mcp.tools import Caller, ToolFailure, ToolService, _num, _product_ref, clean_text

MAX_LINE_PRICE = 1000
OLD_DAYS = 90
STORE_NAME = 60
# Ticket (spec P1) : bornes d'un ticket bien lu
TICKET_LINES = 100
TICKET_MAX_TOTAL = 3000
TICKET_MAX_AGE = 60
LINE_MIN, LINE_MAX = -200, 1000
LABEL = 80
TOTAL_TOLERANCE = 0.05
SCAN_BEFORE, SCAN_AFTER = 3, 1  # entrée scannée sans prix : de 3 jours avant le ticket à 1 jour après
ACTIONS = ("ajouter", "completer", "prix_seulement", "ignorer")
WRITTEN = ("ajoute", "complete", "prix_note", "partiel")
PLAUSIBLE_PER_KG = 200  # €/kg ou €/l : au-delà, la ligne est signalée « à vérifier »
TICKET_LINES_PER_ACTION = 10
SPLIT_MAX_AGE = 180  # jours : au-delà, un prix n'entre pas dans la répartition
SPLIT_MAX_ITEMS = 60
_MONTH = re.compile(r"(20[0-9]{2})-(0[1-9]|1[0-2])")


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
        return rows + [r for r in self.store.prices_for([product_id]) if r["prix"] > 0]

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

    # --- importer_ticket (P1) -------------------------------------------------------------------------
    def _ticket_input(self, magasin: object, day: object, total: object, lignes: object) -> dict:
        """Ticket lu par l'assistant : tout ce qui sort des bornes est refusé (ticket mal lu), rien n'est écrit."""
        shop = self._master_name(magasin)
        text = clean_text(day, 10) if isinstance(day, str) else ""
        try:
            parsed = date.fromisoformat(text) if len(text) == 10 else None
        except ValueError:
            parsed = None
        today = self._today()
        if parsed is None or parsed > today or (today - parsed).days > TICKET_MAX_AGE:
            raise ToolFailure(f"date doit être la date du ticket (AAAA-MM-JJ), ni future ni vieille de plus de {TICKET_MAX_AGE} jours.")
        if not _is_amount(total) or not 0 <= total <= TICKET_MAX_TOTAL:  # type: ignore[operator]
            raise ToolFailure(f"total doit être le montant payé, entre 0 et {TICKET_MAX_TOTAL} €.")
        if not isinstance(lignes, list) or not 1 <= len(lignes) <= TICKET_LINES:
            raise ToolFailure(f"lignes doit contenir de 1 à {TICKET_LINES} lignes du ticket.")
        lines = []
        for number, raw in enumerate(lignes, start=1):
            if not isinstance(raw, dict):
                raise ToolFailure(f"ligne {number} : format invalide.")
            label = clean_text(raw.get("libelle"), LABEL) if isinstance(raw.get("libelle"), str) else ""
            if not label:
                raise ToolFailure(f"ligne {number} : libellé absent.")
            price = raw.get("prix")
            if not _is_amount(price) or not LINE_MIN <= price <= LINE_MAX:  # type: ignore[operator]
                raise ToolFailure(f"ligne {number} : prix entre {LINE_MIN} et {LINE_MAX} € attendu.")
            quantity = raw.get("quantite")
            if quantity is not None and (not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 50):
                if not (isinstance(quantity, float) and quantity.is_integer() and 1 <= quantity <= 50):
                    raise ToolFailure(f"ligne {number} : quantite doit être un nombre entier de 1 à 50.")
                quantity = int(quantity)
            weight = raw.get("poids_kg")
            if weight is not None and (not _is_amount(weight) or not 0.001 <= weight <= 50):
                raise ToolFailure(f"ligne {number} : poids_kg doit être compris entre 0,001 et 50.")
            lines.append({"ligne": number, "libelle": label, "prix": round(float(price), 2), "quantite": quantity,
                          "poids_kg": float(weight) if weight is not None else None})
        return {"magasin": shop, "date": parsed.isoformat(), "total": round(float(total), 2), "lignes": lines}

    @staticmethod
    def _choices(choix: object, numbers: set[int]) -> dict[int, int | str]:
        if choix is None:
            return {}
        if not isinstance(choix, dict):
            raise ToolFailure("choix doit associer un numéro de ligne à un identifiant de produit ou à une action.")
        out: dict[int, int | str] = {}
        for key, value in choix.items():
            number = int(key) if isinstance(key, str) and key.isdigit() else key if isinstance(key, int) and not isinstance(key, bool) else None
            if number not in numbers:
                raise ToolFailure(f"choix : la ligne « {clean_text(key, 10)} » n'est pas un article de ce ticket.")
            if isinstance(value, bool) or not (isinstance(value, int) and value > 0 or value in ACTIONS):
                raise ToolFailure(f"choix : ligne {number}, identifiant de produit ou action ({', '.join(ACTIONS)}) attendu.")
            out[number] = value
        return out

    @staticmethod
    def _candidates(entries: list[dict], product_id: int, day: str) -> list[dict]:
        """Entrées scannées sans prix autour de la date du ticket, les plus récentes d'abord."""
        start = (date.fromisoformat(day) - timedelta(days=SCAN_BEFORE)).isoformat()
        end = (date.fromisoformat(day) + timedelta(days=SCAN_AFTER)).isoformat()
        found = [
            e for e in entries
            if int(e.get("product_id", -1)) == product_id and float(e.get("amount") or 0) > 0
            and not float(e.get("price") or 0) and start <= str(e.get("purchased_date") or "")[:10] <= end
        ]
        return sorted(found, key=lambda e: (str(e.get("purchased_date")), int(e["id"])), reverse=True)

    def _plan_line(self, article: dict, shop_id: int | None, choice: int | str | None, products: dict[int, dict],
                   units: dict[int, str], conversions: list[dict], entries: list[dict], day: str) -> dict:
        """Association et action d'un article ; rien n'est associé d'office sans mémoire ou choix de la personne."""
        label = fold(article["libelle"])
        line = {"ligne": article["ligne"], "libelle": article["libelle"], "prix": article["prix"], "remise": article["remise"],
                "origine": "inconnu", "produit": None, "candidats": [], "action": None}
        remembered = self.store.label_product(shop_id, label) if shop_id is not None else None
        if remembered is not None and remembered in products:  # numéro réutilisé par un produit recréé : oublié
            created = products[remembered].get("row_created_timestamp")
            remembered = self.store.label_product(shop_id, label, str(created) if created else None)
        product_id = choice if isinstance(choice, int) else remembered
        if isinstance(choice, int):
            line["origine"] = "choix"
        elif remembered is not None:
            line["origine"] = "memoire"
        product = products.get(product_id) if product_id is not None else None
        if product is None:
            if line["origine"] == "memoire":  # produit supprimé depuis : la mémoire ne vaut plus rien
                line["origine"] = "inconnu"
            if product_id is not None and isinstance(choice, int):
                line["raison"] = "produit introuvable ou inactif"
            line["candidats"] = [{"id": int(p["id"]), "nom": clean_text(p["name"])} for p in suggest_products(article["libelle"], list(products.values()))]
            if line["candidats"] and line["origine"] != "choix":
                line["origine"] = "a_choisir"
            if choice == "ignorer":
                line["action"] = "ignorer"
            return line
        line["produit"] = _product_ref(product)
        if choice == "ignorer":
            line["action"] = "ignorer"
            return line
        if article["prix"] <= 0:
            line.update(action="ignorer", raison="article gratuit (0 €) : aucun prix enregistré ; ajoutez-le au stock dans l'app si besoin")
            return line
        amount = stock_amount(article, product, units, conversions)
        if not amount:
            purchase = int(product.get("qu_id_purchase") or product["qu_id_stock"])
            reason = ("poids absent : article vendu au poids ou au volume, indiquez poids_kg" if not article.get("poids_kg")
                      and metric_kind(units.get(purchase, "")) is not None
                      else "unité à préciser : conversion vers l'unité de stock inconnue (definir_conditionnement)")
            line.update(action="ignorer", raison=reason)
            return line
        candidates = self._candidates(entries, int(product["id"]), day)
        action = choice if isinstance(choice, str) else ("completer" if candidates else "ajouter")
        if action == "completer" and not candidates:
            action = "ajouter"
        unit_price = article["prix"] / amount
        normalized = normalized_price(unit_price, product, units, conversions)
        line.update(action=action, quantite_stock=amount, prix_unite_stock=round(unit_price, 4), prix_normalise=normalized,
                    a_verifier=normalized["unite"] in ("kg", "l") and normalized["valeur"] > PLAUSIBLE_PER_KG)
        if action == "completer":
            scanned = sum(float(e["amount"]) for e in candidates)
            line.update(a_completer=_num(min(scanned, amount)), non_rapproche=_num(max(0.0, amount - scanned)))
        return line

    async def _ticket_plan(self, client: GrocyClient, ticket: dict, shop_id: int | None, choices: dict) -> tuple[list[dict], list[str], dict]:
        articles, warnings = apply_discounts(ticket["lignes"])
        products = {int(p["id"]): p for p in await client.products() if int(p.get("active", 1)) == 1}
        units, conversions = await self._units_and_conversions(client)
        entries = await client.stock_entries()
        plan = [self._plan_line(a, shop_id, choices.get(a["ligne"]), products, units, conversions, entries, ticket["date"])
                for a in articles]
        for line in plan:
            if line.get("a_verifier"):
                warnings.append(f"ligne {line['ligne']} : {line['prix_normalise']['valeur']} €/{line['prix_normalise']['unite']}, "
                                "prix invraisemblable — vérifiez la quantité, le poids ou le conditionnement du produit")
        return plan, warnings, {"products": products, "entries": entries}

    async def importer_ticket(
        self,
        caller: Caller,
        magasin: str | None = None,
        date: str | None = None,  # noqa: A002 — nom du paramètre exposé à l'assistant
        total: float | None = None,
        lignes: list | None = None,
        jeton_confirmation: str | None = None,
        choix: dict | None = None,
    ) -> dict:
        tool = "importer_ticket"
        if not jeton_confirmation:
            return await self._grocy(caller, lambda client: self._ticket_preview(caller, client, self._ticket_input(magasin, date, total, lignes), {}))
        token = clean_text(jeton_confirmation, 100)
        payload = self.store.peek_confirmation(token, caller.grocy_user_id, tool)
        if payload is None:
            raise ToolFailure("Jeton de confirmation invalide, déjà utilisé ou expiré (5 minutes). Redemandez un aperçu.")
        ticket = json.loads(payload)
        known = {int(k): v for k, v in ticket.pop("choix", {}).items()}
        choices = self._choices(choix, {a["ligne"] for a in apply_discounts(ticket["lignes"])[0]})
        merged = {**known, **choices}
        if any(isinstance(v, int) and known.get(k) != v for k, v in choices.items()):
            # Nouveau produit choisi : second aperçu chiffré (quantité, prix, action) avant toute écriture
            self._take_confirmation(caller, tool, token)
            return await self._grocy(caller, lambda client: self._ticket_preview(caller, client, ticket, merged))
        count = self._ticket_cost(ticket)
        if self._quota_left(caller) < count:
            self._consume_quota(caller, count)  # refuse avant de consommer le jeton
        self._take_confirmation(caller, tool, token)  # consommé seulement avec des choix valides et du quota
        return await self._ticket_confirm(caller, ticket, merged, count)

    @staticmethod
    def _ticket_cost(ticket: dict) -> int:
        """Un ticket compte une action par tranche de 10 lignes dans le quota (100 lignes : 10 actions)."""
        return max(1, math.ceil(len(ticket["lignes"]) / TICKET_LINES_PER_ACTION))

    async def _ticket_preview(self, caller: Caller, client: GrocyClient, ticket: dict, choices: dict) -> dict:
        shop = match_store(await client.shopping_locations(), ticket["magasin"])
        cents = round(ticket["total"] * 100)
        if shop and self.store.receipt_exists(int(shop["id"]), ticket["date"], cents):
            return {"statut": "deja_importe", "message": f"Ce ticket ({_store_name(shop)}, {ticket['date']}, "
                    f"{ticket['total']:.2f} €) a déjà été importé : rien n'est fait."}
        plan, warnings, _ = await self._ticket_plan(client, ticket, int(shop["id"]) if shop else None, choices)
        gap = check_total(ticket["lignes"], ticket["total"])
        if abs(gap) > TOTAL_TOLERANCE:
            warnings.append(f"la somme des lignes diffère du total de {gap:+.2f} € : vérifiez la lecture du ticket")
        payload = {**ticket, "choix": {str(k): v for k, v in choices.items()}}
        return {
            "statut": "apercu",
            "message": "Aperçu : rien n'a été écrit. Montrez-le à la personne ; pour chaque ligne « a_choisir » ou "
            "« inconnu », demandez le produit (ou « ignorer »). Rappelez importer_ticket avec jeton_confirmation et "
            "choix = {numéro de ligne: id du produit ou action} : un nouveau produit choisi donne un aperçu chiffré, à "
            "confirmer ensuite avec le nouveau jeton (sans nouveau produit). Une ligne sans produit est ignorée.",
            "magasin": {"nom": _store_name(shop) if shop else ticket["magasin"], "nouveau": shop is None},
            "date": ticket["date"],
            "total": ticket["total"],
            "ecart_total": gap,
            "avertissements": warnings,
            "lignes": plan,
            "jeton_confirmation": self._new_confirmation(caller, "importer_ticket", payload),
        }

    async def _ticket_confirm(self, caller: Caller, ticket: dict, choices: dict, count: int) -> dict:
        uid = caller.grocy_user_id
        state: dict = {"transactions": [], "local": [], "receipt": None}

        async def write_line(client: GrocyClient, line: dict, shop_id: int, entries: list[dict]) -> str:
            product_id = int(line["produit"]["id"])
            unit_price, amount = line["prix_unite_stock"], line["quantite_stock"]
            if line["action"] == "prix_seulement":
                price_id = self.store.add_price(uid, product_id=product_id, location_id=shop_id, price=unit_price,
                                                day=ticket["date"], source="ticket")
                state["local"].append(f"prix:{price_id}")
                return "prix_note"
            if line["action"] == "completer":
                # seules les entrées scannées sont complétées : le reste n'est jamais acheté en silence
                remaining = amount
                for entry in self._candidates(entries, product_id, ticket["date"]):
                    if remaining <= 1e-6:
                        break
                    body = {"amount": float(entry["amount"]), "best_before_date": entry.get("best_before_date"),
                            "location_id": entry.get("location_id"), "shopping_location_id": shop_id, "price": unit_price,
                            "open": int(entry.get("open") or 0), "purchased_date": str(entry.get("purchased_date"))[:10]}
                    rows = await client.edit_stock_entry(int(entry["id"]), body)
                    self._remember_tx(state, rows)
                    entry["price"] = unit_price  # une autre ligne du ticket ne complète pas deux fois la même entrée
                    remaining -= float(entry["amount"])
                return "complete"
            rows = await client.purchase(product_id, amount, price=unit_price, purchased_date=ticket["date"],
                                         shopping_location_id=shop_id)
            self._remember_tx(state, rows)
            return "ajoute"

        async def run(client: GrocyClient) -> dict:
            shop, shop_created = await self._store_for(caller, client, ticket["magasin"])
            shop_id = int(shop["id"])
            receipt = self.store.reserve_receipt(shop_id, ticket["date"], round(ticket["total"] * 100))
            if receipt is None:
                return {"statut": "deja_importe", "message": "Ce ticket a déjà été importé : rien n'est fait."}
            state["receipt"] = receipt
            plan, warnings, context = await self._ticket_plan(client, ticket, shop_id, choices)
            results, stop = [], None
            for line in plan:
                base = {"ligne": line["ligne"], "libelle": line["libelle"]}
                if stop:
                    results.append({**base, "resultat": "non_traite", "raison": stop})
                    continue
                if line["action"] in (None, "ignorer"):
                    results.append({**base, "resultat": "ignore", "raison": line.get("raison") or "aucun produit choisi"})
                    continue
                before = len(state["transactions"])
                try:
                    outcome = await write_line(client, line, shop_id, context["entries"])
                except (GrocyRequestError, GrocyForbidden) as error:
                    reason = f"refusé par Grocy : {clean_text(error.message, 200)}"
                    partial = len(state["transactions"]) > before
                    results.append({**base, "resultat": "partiel" if partial else "ignore", "raison": reason})
                    if not partial:
                        continue
                    outcome = None
                except GrocyUnavailable:
                    stop = "Grocy ne répond plus : ligne non traitée"
                    results.append({**base, "resultat": "incertain", "raison": "Grocy n'a pas répondu : cette ligne a peut-être "
                                    "été enregistrée ; vérifiez-la dans Grocy"})
                    continue
                product = context["products"][int(line["produit"]["id"])]
                product_created = product.get("row_created_timestamp")
                self.store.save_label(shop_id, fold(line["libelle"]), int(product["id"]),
                                      str(product_created) if product_created else None, receipt_id=receipt)
                if outcome is not None:
                    result = {**base, "resultat": outcome, "produit": line["produit"], "prix_unite_stock": line["prix_unite_stock"]}
                    if outcome == "complete":
                        result["non_rapproche"] = line.get("non_rapproche", 0)
                    results.append(result)
                self.audit.write("ticket_ligne", compte_id=uid, ligne=line["ligne"], resultat=outcome or "partiel",
                                 produit_id=line["produit"]["id"])
            written = bool(state["transactions"] or state["local"])
            message = f"{sum(1 for r in results if r['resultat'] in WRITTEN)} ligne(s) du ticket enregistrée(s) chez {_store_name(shop)}."
            if written:
                message += " annuler_derniere_action annule tout le ticket."
            if any(r["resultat"] == "incertain" for r in results):
                message += (" Ligne « incertain » : Grocy n'a pas répondu, elle a peut-être été enregistrée ; vérifiez-la "
                            "dans Grocy (annuler_derniere_action ne l'annule pas).")
            if any(r.get("non_rapproche") for r in results):
                message += " « non_rapproche » : quantités du ticket sans article scanné, non ajoutées au stock."
            return {
                "statut": "ok" if written else "rien_importe",
                "message": message,
                "magasin_cree": shop_created,
                "avertissements": warnings,
                "lignes": results,
                "transaction_id": " ".join(state["transactions"]) or None,
            }

        try:
            return await self._execute(caller, "importer_ticket", {"lignes": len(ticket["lignes"]), "confirmation": True}, run, count=count)
        finally:
            # Ce qui a été écrit reste annulable d'un bloc, même après une panne ; un ticket sans écriture peut être refait
            if state["receipt"] is not None:
                if state["transactions"] or state["local"]:
                    tokens = [*state["transactions"], *state["local"], f"ticket:{state['receipt']}"]
                    self.store.record_undo(uid, " ".join(tokens), "importer_ticket", f"ticket {ticket['magasin']} du {ticket['date']}")
                else:
                    self.store.delete_receipt(state["receipt"])

    @staticmethod
    def _remember_tx(state: dict, rows: list[dict] | None) -> None:
        if rows and rows[0].get("transaction_id") and str(rows[0]["transaction_id"]) not in state["transactions"]:
            state["transactions"].append(str(rows[0]["transaction_id"]))

    # --- repartir_courses (P4) ------------------------------------------------------------------------
    async def repartir_courses(self, caller: Caller, max_magasins: int = 2, liste: int = 1) -> dict:
        if not isinstance(max_magasins, int) or isinstance(max_magasins, bool) or not 1 <= max_magasins <= 3:
            raise ToolFailure("max_magasins doit être 1, 2 ou 3.")
        if not isinstance(liste, int) or isinstance(liste, bool) or liste < 1:
            raise ToolFailure("liste doit être le numéro d'une liste de courses (1 par défaut).")

        async def run(client: GrocyClient) -> dict:
            listed = [i for i in await client.shopping_list()
                      if not int(i.get("done") or 0) and int(i.get("shopping_list_id") or 1) == liste]
            notes = [clean_text(i.get("note"), 60) for i in listed if i.get("product_id") is None and i.get("note")]
            names = {int(p["id"]): clean_text(p["name"]) for p in await client.products()}
            shops = {int(s["id"]): s for s in await client.shopping_locations()}
            since = (self._today() - timedelta(days=SPLIT_MAX_AGE)).isoformat()
            wanted: dict[int, float] = {}
            for item in listed:
                if item.get("product_id") is not None:
                    pid = int(item["product_id"])
                    wanted[pid] = wanted.get(pid, 0) + float(item.get("amount") or 1)
            items: dict[str, dict[int, float]] = {}
            overflow = [str(pid) for pid in list(wanted)[SPLIT_MAX_ITEMS:]]  # au-delà : listés sans prix, jamais perdus
            for pid in list(wanted)[:SPLIT_MAX_ITEMS]:
                recent = [r for r in await self._known_prices(client, pid) if r["date"] >= since]
                items[str(pid)] = {loc: row["prix"] * wanted[pid] for loc, row in latest_by_store(recent).items()}
            split = best_split(items, max_magasins)
            by_store: dict[int, list[dict]] = {}
            for key, loc in split["affectation"].items():
                by_store.setdefault(loc, []).append(
                    {"produit": names.get(int(key), f"#{key}"), "quantite": _num(wanted[int(key)]), "prix_estime": round(items[key][loc], 2)}
                )
            stores = [
                {"magasin": _store_name(shops.get(loc)) or f"magasin {loc}", "articles": rows,
                 "sous_total": round(sum(r["prix_estime"] for r in rows), 2)}
                for loc, rows in sorted(by_store.items())
            ]
            reference = split["reference"]
            return {
                "statut": "ok",
                "max_magasins": max_magasins,
                "par_magasin": stores,
                "total": split["total"],
                "economie": {"par_rapport_a": _store_name(shops.get(reference["magasin"])),
                             "montant": round(reference["total"] - split["total"], 2)} if reference else None,
                "sans_prix": [names.get(int(key), f"#{key}") for key in split["non_couverts"] + overflow],
                "sans_produit": notes,
                "message": f"Estimation d'après les derniers prix connus (moins de {SPLIT_MAX_AGE} jours) ; l'économie est "
                "calculée sur les mêmes articles que le meilleur magasin seul.",
            }

        return await self._grocy(caller, run)

    # --- depenses (P5) -------------------------------------------------------------------------------
    def _months(self, mois: object, derniers_mois: object) -> list[str]:
        if mois is not None:
            if not isinstance(mois, str) or not _MONTH.fullmatch(mois):
                raise ToolFailure("mois doit être au format AAAA-MM.")
            return [mois]
        if not isinstance(derniers_mois, int) or isinstance(derniers_mois, bool) or not 1 <= derniers_mois <= 12:
            raise ToolFailure("derniers_mois doit être un entier de 1 à 12.")
        today = self._today()
        months = []
        for back in range(derniers_mois - 1, -1, -1):
            index = today.year * 12 + today.month - 1 - back
            months.append(f"{index // 12:04d}-{index % 12 + 1:02d}")
        return months

    @staticmethod
    def _purchases(log: list[dict]) -> list[dict]:
        """Achats non annulés avec leur prix (celui de la dernière modification s'il a été complété ensuite)."""
        live = [r for r in log if not int(r.get("undone") or 0)]
        edits: dict[str, dict] = {}
        for row in live:
            if row.get("transaction_type") == "stock-edit-new":
                stock = str(row.get("stock_id"))
                if stock not in edits or int(row["id"]) > int(edits[stock]["id"]):
                    edits[stock] = row
        found = []
        for row in live:
            if row.get("transaction_type") != "purchase":
                continue
            latest = edits.get(str(row.get("stock_id")), row)
            price = _to_float(latest.get("price"))
            amount = _to_float(row.get("amount"))
            if not price or price <= 0 or not amount or amount <= 0:
                continue
            found.append({"product_id": int(row["product_id"]), "location_id": latest.get("shopping_location_id"),
                          "date": str(latest.get("purchased_date") or row.get("purchased_date") or "")[:10],
                          "prix": price, "montant": amount * price})
        return found

    async def depenses(self, caller: Caller, mois: str | None = None, derniers_mois: int = 3) -> dict:
        months = self._months(mois, derniers_mois)

        async def run(client: GrocyClient) -> dict:
            purchases = self._purchases(await client.stock_log())
            shops = {int(s["id"]): s for s in await client.shopping_locations()}
            names = {int(p["id"]): clean_text(p["name"]) for p in await client.products()}

            def shop(loc: object) -> str:
                return _store_name(shops.get(int(loc))) if loc is not None else "magasin non indiqué"

            report = []
            for month in months:
                totals: dict[str, float] = {}
                for p in purchases:
                    if p["date"][:7] == month:
                        totals[shop(p["location_id"])] = totals.get(shop(p["location_id"]), 0) + p["montant"]
                report.append({"mois": month, "total": round(sum(totals.values()), 2),
                               "par_magasin": [{"magasin": k, "total": round(v, 2)} for k, v in sorted(totals.items(), key=lambda kv: -kv[1])]})
            rows = [p for p in purchases if p["location_id"] is not None] + self.store.prices_for(None)
            rises = [
                {"produit": names.get(r["product_id"], f"#{r['product_id']}"), "magasin": shop(r["location_id"]),
                 "avant": r["avant"], "apres": r["apres"], "hausse_pct": r["hausse_pct"], "date": r["date"]}
                for r in price_rises(rows)
                if str(r["date"])[:7] in months
            ]
            return {
                "statut": "ok",
                "par_mois": report,
                "total": round(sum(m["total"] for m in report), 2),
                "hausses": rises,
                "message": "Dépenses d'après les achats enregistrés dans Grocy avec un prix ; hausses de plus de 10 % "
                "entre deux prix d'un même produit dans un même magasin.",
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
