"""Passage de conditionnement : contenance des produits et conversions d'unités (spec conditionnement C1 à C7)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from grocy_mcp.audit import AuditLog
from grocy_mcp.grocy import GrocyClient, GrocyError, GrocyRequestError
from grocy_mcp.packaging import (
    AMBIGUOUS, Content, Decision, clean_name, metric_kind, own_conversions, parse_content, plan_product,
)
from grocy_mcp.sources import FoodSources, SourceError

OFF_TTL = 7 * 86400  # C7 : Open Food Facts interrogé au plus une fois par code-barres et par 7 jours
MAX_LOOKUPS_PER_PASS = 30  # au-delà, les produits sont reportés au passage suivant (revue M1)
# Réponse d'Open Food Facts illisible ou piégée : traitée comme une panne, jamais fatale au passage (revue I2)
_OFF_FAILURES = (SourceError, ValueError, TypeError, OverflowError, RecursionError, AttributeError, KeyError)
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


class JobState:
    """Produits déjà traités par le passage (écrits ou écartés à la main) : une contenance écrite puis supprimée par
    une personne n'est jamais recréée."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None
        self.done: set[int] = set()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            ids = data.get("traites") if isinstance(data, dict) else None
            self.done = {int(i) for i in ids if isinstance(i, int) and not isinstance(i, bool)} if isinstance(ids, list) else set()

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"traites": sorted(self.done)}, f)
        os.replace(tmp, self.path)

    def mark(self, product_id: int) -> None:
        if int(product_id) not in self.done:
            self.done.add(int(product_id))
            self._save()

    def ignore(self, product_ids: list[int]) -> None:
        self.done |= {int(i) for i in product_ids}
        self._save()


async def _off_quantity(sources: FoodSources, cache: OffCache, code: str) -> str | None:
    known, quantity = cache.get(code)
    if known:
        return quantity
    try:
        quantity = await sources.off_quantity(code)
    except _OFF_FAILURES:
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


def needs_lookup(product: dict, conversions: list[dict], codes: list[str], cache: OffCache) -> bool:
    """Vrai si décider de ce produit demande un appel réseau à Open Food Facts."""
    return bool(codes) and not own_conversions(int(product["id"]), conversions) and not cache.get(codes[0])[0]


async def decide(product: dict, units: dict[int, str], conversions: list[dict], codes: list[str],
                 sources: FoodSources | None, cache: OffCache, ours: bool = False) -> Decision:
    """Décision pour un produit : nom, puis Open Food Facts, puis poids de la partie nutrition (repli).

    Un produit qui a déjà ses propres conversions n'interroge pas Open Food Facts : seul le cache sert, pour compléter
    une écriture interrompue (revue I1).
    """
    candidates: list[tuple[str, object]] = [("nom", parse_content(product.get("name")))]
    if codes:
        if own_conversions(int(product["id"]), conversions) or sources is None:
            known, quantity = cache.get(codes[0])
            quantity = quantity if known else None
        else:
            quantity = await _off_quantity(sources, cache, codes[0])
        if quantity:
            candidates.append(("Open Food Facts", parse_content(quantity)))
    stock_is_metric = metric_kind(units.get(int(product["qu_id_stock"]), "")) is not None
    # le poids nutrition d'un produit stocké en g ou en kg est celui de l'unité (1 g, 1000 g), pas du paquet (revue C1)
    if all(content is None for _, content in candidates) and not stock_is_metric:
        weight = _nutrition_weight(product)
        if weight is not None:
            candidates.append(("nutrition", Content(1, weight, "Gramme")))
    return plan_product(product, units, conversions, candidates, ours=ours)


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
    state: JobState | None = None,
) -> dict:
    """Écrit les conversions certaines ; ne touche ni au stock, ni aux noms, ni aux conversions existantes (C6)."""
    products, units, conversions, codes = await load_catalog(client)
    account = (await client.current_user()).id if audit is not None and not dry_run else None
    lines, written, lookups = [], 0, 0
    for product in products:
        pid, name = int(product["id"]), clean_name(product.get("name"))
        codes_of = codes.get(pid, [])
        if state is not None and pid in state.done and not own_conversions(pid, conversions):
            # déjà traité et plus aucune conversion : retirée ou écartée par une personne, jamais recréée
            lines.append({"produit": name, "statut": "retire", "source": None, "contenance": None})
            continue
        if sources is not None and needs_lookup(product, conversions, codes_of, cache):
            if lookups >= MAX_LOOKUPS_PER_PASS:
                lines.append({"produit": name, "statut": "reporte", "source": None, "contenance": None})
                continue
            lookups += 1
        decision = await decide(product, units, conversions, codes_of, sources, cache, ours=state is not None and pid in state.done)
        status = decision.status
        if status == "a_ecrire" and not dry_run:
            created: list[list] = []
            try:
                for source_unit, target_unit, factor in decision.conversions:
                    await client.create_object(
                        "quantity_unit_conversions",
                        {"product_id": pid, "from_qu_id": source_unit, "to_qu_id": target_unit, "factor": factor},
                    )
                    created.append([source_unit, target_unit, factor])
                if decision.purchase_unit is not None:
                    await client.set_purchase_unit(pid, decision.purchase_unit)
            except GrocyError as error:
                if created:  # écriture interrompue : tracée, complétée au prochain passage
                    if state is not None:
                        state.mark(pid)
                    if audit is not None:
                        audit.write("conditionnement_partiel", compte_id=account, produit_id=pid, conversions=created)
                if not isinstance(error, GrocyRequestError):
                    raise  # clé refusée ou Grocy injoignable : tout le passage s'arrête
                status = "erreur"  # les autres produits continuent
            else:
                status, written = "ecrit", written + 1
                if state is not None:
                    state.mark(pid)
                if audit is not None:
                    audit.write(
                        "conditionnement", compte_id=account, produit_id=pid, source=decision.source,
                        contenance=describe(decision.content),
                        conversions=[[a, b, f] for a, b, f in decision.conversions], unite_achat=decision.purchase_unit,
                    )
        lines.append({"produit": name, "statut": status, "source": decision.source, "contenance": describe(decision.content)})
    return {"ecrits": written, "produits": lines}


__all__ = ["AMBIGUOUS", "OffCache", "decide", "describe", "load_catalog", "JobState", "needs_lookup", "run_packaging"]
