"""Passage de conditionnement : contenance des produits et conversions d'unités (spec conditionnement C1 à C7)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from grocy_mcp.audit import AuditLog
from grocy_mcp.grocy import GrocyClient, GrocyRequestError
from grocy_mcp.packaging import AMBIGUOUS, Content, Decision, clean_name, own_conversions, parse_content, plan_product
from grocy_mcp.sources import FoodSources, SourceError

OFF_TTL = 7 * 86400  # C7 : Open Food Facts interrogé au plus une fois par code-barres et par 7 jours
NUTRI_WEIGHT = "nutri_poids_unite_g"


class OffCache:
    """Réponses d'Open Food Facts déjà obtenues (contenance ou absence) ; une panne n'est jamais retenue."""

    def __init__(self, path: Path | str | None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._data: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                self._data = {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("t"), int | float)}

    def get(self, code: str) -> tuple[bool, str | None]:
        row = self._data.get(code)
        if row is None or self._clock() - row["t"] > OFF_TTL:
            return False, None
        quantity = row.get("q")
        return True, quantity if isinstance(quantity, str) else None

    def put(self, code: str, quantity: str | None) -> None:
        self._data[code] = {"t": self._clock(), "q": quantity}
        if self.path is None:
            return
        now = self._clock()
        keep = {k: v for k, v in self._data.items() if now - v["t"] <= OFF_TTL}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(keep, f)
        os.replace(tmp, self.path)


async def _off_quantity(sources: FoodSources, cache: OffCache, code: str) -> str | None:
    known, quantity = cache.get(code)
    if known:
        return quantity
    try:
        quantity = await sources.off_quantity(code)
    except SourceError:
        return None  # Review Focus 4 : réessayé au prochain passage
    cache.put(code, quantity)
    return quantity


def _nutrition_weight(product: dict) -> float | None:
    raw = (product.get("userfields") or {}).get(NUTRI_WEIGHT)
    try:
        weight = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return weight if 0 < weight <= 50_000 else None


def describe(content: object) -> str | None:
    if not isinstance(content, Content):
        return None
    amount = f"{content.amount:g}".replace(".", ",")
    return f"{content.count} × {amount} {content.unit}" if content.count > 1 else f"{amount} {content.unit}"


async def decide(product: dict, units: dict[int, str], conversions: list[dict], codes: list[str],
                 sources: FoodSources | None, cache: OffCache) -> Decision:
    """Décision pour un produit : nom, puis Open Food Facts, puis poids de la partie nutrition (repli)."""
    if own_conversions(int(product["id"]), conversions):
        return Decision("deja")  # C4 : sans même interroger Open Food Facts
    candidates: list[tuple[str, object]] = [("nom", parse_content(product.get("name")))]
    if sources is not None and codes:
        quantity = await _off_quantity(sources, cache, codes[0])
        if quantity:
            candidates.append(("Open Food Facts", parse_content(quantity)))
    if all(content is None for _, content in candidates):
        weight = _nutrition_weight(product)
        if weight is not None:
            candidates.append(("nutrition", Content(1, weight, "Gramme")))
    return plan_product(product, units, conversions, candidates)


async def load_catalog(client: GrocyClient) -> tuple[list[dict], dict[int, str], list[dict], dict[int, list[str]]]:
    products = [p for p in await client.products() if str(p.get("active", 1)) not in ("0", "")]
    units = {int(u["id"]): str(u["name"]) for u in await client.quantity_units()}
    conversions = await client.quantity_unit_conversions() or []
    codes: dict[int, list[str]] = {}
    for row in await client.product_barcodes() or []:
        if row.get("product_id") not in (None, ""):
            codes.setdefault(int(row["product_id"]), []).append(str(row["barcode"]))
    return sorted(products, key=lambda p: int(p["id"])), units, conversions, codes


async def run_packaging(
    client: GrocyClient,
    sources: FoodSources | None,
    cache: OffCache,
    *,
    dry_run: bool = False,
    audit: AuditLog | None = None,
) -> dict:
    """Écrit les conversions certaines ; ne touche ni au stock, ni aux noms, ni aux conversions existantes (C6)."""
    products, units, conversions, codes = await load_catalog(client)
    account = (await client.current_user()).id if audit is not None and not dry_run else None
    lines, written = [], 0
    for product in products:
        pid, name = int(product["id"]), clean_name(product.get("name"))
        decision = await decide(product, units, conversions, codes.get(pid, []), sources, cache)
        status = decision.status
        if status == "a_ecrire" and not dry_run:
            try:
                for source_unit, target_unit, factor in decision.conversions:
                    await client.create_object(
                        "quantity_unit_conversions",
                        {"product_id": pid, "from_qu_id": source_unit, "to_qu_id": target_unit, "factor": factor},
                    )
                if decision.purchase_unit is not None:
                    await client.set_purchase_unit(pid, decision.purchase_unit)
            except GrocyRequestError:
                status = "erreur"  # les autres produits continuent ; les erreurs de clé ou de réseau arrêtent tout
            else:
                status, written = "ecrit", written + 1
                if audit is not None:
                    audit.write(
                        "conditionnement", compte_id=account, produit_id=pid, source=decision.source,
                        contenance=describe(decision.content),
                        conversions=[[a, b, f] for a, b, f in decision.conversions], unite_achat=decision.purchase_unit,
                    )
        lines.append({"produit": name, "statut": status, "source": decision.source, "contenance": describe(decision.content)})
    return {"ecrits": written, "produits": lines}


__all__ = ["AMBIGUOUS", "OffCache", "decide", "describe", "load_catalog", "run_packaging"]
