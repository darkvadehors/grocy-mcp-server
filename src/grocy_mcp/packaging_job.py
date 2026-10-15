"""Passage de conditionnement : contenance des produits et conversions d'unités (spec conditionnement C1 à C7)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from grocy_mcp.audit import AuditLog
from grocy_mcp.enrich import FIELDS, NUTRI_BY_KEY, NUTRI_WEIGHT, missing_fields, plan_enrichment
from grocy_mcp.grocy import GrocyClient, GrocyError, GrocyRequestError
from grocy_mcp.packaging import (
    AMBIGUOUS, Content, Decision, clean_name, metric_kind, own_conversions, parse_content, plan_product,
)
from grocy_mcp.sources import FoodSources, SourceError

_NO_RECORD = object()
OFF_TTL = 7 * 86400  # C7 : Open Food Facts interrogé au plus une fois par code-barres et par 7 jours
LOOKUP_PAUSE = 1.0  # secondes entre deux requêtes Open Food Facts d'un passage (débit limité, réponses 429)
MAX_LOOKUPS_PER_PASS = 30  # au-delà, les produits sont reportés au passage suivant (revue M1)
# Réponse d'Open Food Facts illisible ou piégée : traitée comme une panne, jamais fatale au passage (revue I2)
_OFF_FAILURES = (SourceError, ValueError, TypeError, OverflowError, RecursionError, AttributeError, KeyError)


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

    def record(self, code: str) -> tuple[bool, dict | None]:
        """Fiche complète (enrichissement) ; une entrée d'avant l'enrichissement (contenance seule) est inconnue."""
        row = self._data.get(code)
        if row is None or "r" not in row or self._clock() - row["t"] > OFF_TTL:
            return False, None
        record = row["r"]
        return True, record if isinstance(record, dict) else None

    def put(self, code: str, quantity: str | None, record: object = _NO_RECORD) -> None:
        self._data[code] = {"t": self._clock(), "q": quantity}
        if record is not _NO_RECORD:
            self._data[code]["r"] = record
        self._save()

    def drop_image(self, code: str) -> None:
        """Photo refusée (pas une image, trop lourde…) : plus retéléchargée tant que la fiche est en cache."""
        row = self._data.get(code)
        if row is not None and isinstance(row.get("r"), dict):
            row["r"] = {**row["r"], "img": None}
            self._save()

    def _save(self) -> None:
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
        self.filled: dict[int, set[str]] = {}  # champs remplis par l'enrichissement, jamais remplis à nouveau
        self.created: dict[int, str] = {}  # date de création de la fiche : un numéro réutilisé repart de zéro
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            ids = data.get("traites") if isinstance(data, dict) else None
            self.done = {int(i) for i in ids if isinstance(i, int) and not isinstance(i, bool)} if isinstance(ids, list) else set()
            filled = data.get("enrichis") if isinstance(data, dict) else None
            for key, names in (filled or {}).items() if isinstance(filled, dict) else ():
                if str(key).isdigit() and isinstance(names, list):
                    self.filled[int(key)] = {n for n in names if n in FIELDS}
            created = data.get("crees") if isinstance(data, dict) else None
            for key, stamp in (created or {}).items() if isinstance(created, dict) else ():
                if str(key).isdigit() and isinstance(stamp, str):
                    self.created[int(key)] = stamp

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"traites": sorted(self.done),
                       "enrichis": {str(k): sorted(v) for k, v in sorted(self.filled.items()) if v},
                       "crees": {str(k): v for k, v in sorted(self.created.items())}}, f)
        os.replace(tmp, self.path)

    def mark(self, product_id: int, created: object = None) -> None:
        if isinstance(created, str) and created:
            self.created[int(product_id)] = created
        if int(product_id) not in self.done:
            self.done.add(int(product_id))
            self._save()

    def handled(self, product: dict) -> bool:
        """Lecture seule : produit déjà traité, et pas une autre fiche sous un numéro réutilisé."""
        pid, stamp = int(product["id"]), product.get("row_created_timestamp")
        known = self.created.get(pid)
        return pid in self.done and (known is None or not isinstance(stamp, str) or known == stamp)

    def renew(self, product: dict) -> None:
        """Oublie la mémoire d'un numéro de produit réutilisé (l'app Grocy Mobile choisit elle-même les numéros)."""
        pid, stamp = int(product["id"]), product.get("row_created_timestamp")
        if not isinstance(stamp, str) or not stamp:
            return
        known = self.created.get(pid)
        if known == stamp:
            return
        if known is not None:  # autre fiche sous le même numéro
            self.done.discard(pid)
            self.filled.pop(pid, None)
        elif pid not in self.done and pid not in self.filled:
            return  # rien à retenir pour ce produit
        self.created[pid] = stamp
        self._save()

    def mark_field(self, product_id: int, name: str, created: object = None) -> None:
        if isinstance(created, str) and created:
            self.created[int(product_id)] = created
        self.filled.setdefault(int(product_id), set()).add(name)
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


class _Failed(Exception):
    """Open Food Facts en panne pour ce code pendant ce passage."""


async def _off_record(sources: FoodSources, cache: OffCache, code: str) -> dict | None:
    known, record = cache.record(code)
    if known:
        return record
    try:
        record = await sources.off_record(code)
    except _OFF_FAILURES:
        raise _Failed from None
    cache.put(code, record["q"] if record else None, record)
    return record


def _picture_name(product_id: int, code: str, ext: str) -> str:
    """Nom unique (Grocy n'écrase jamais un fichier) dont l'écriture en base64 ne contient ni « / » ni « + »."""
    for _ in range(64):
        name = f"off-{product_id}-{code}-{int(time.time())}-{secrets.token_hex(3)}.{ext}"
        if not set(base64.b64encode(name.encode()).decode()) & {"/", "+"}:
            return name
    raise ValueError("nom de photo introuvable")


def _num(value: float) -> float | int:
    value = round(float(value), 3)
    return int(value) if value == int(value) else value


async def _enrich(
    client: GrocyClient, sources: FoodSources | None, cache: OffCache, product: dict, code: str, plan,
    state: JobState | None, written: list[str],
) -> None:
    """Écrit le plan champ par champ (catégorie, nutrition, photo) ; chaque champ écrit est mémorisé aussitôt."""
    pid = int(product["id"])

    def done(name: str) -> None:
        written.append(name)
        if state is not None:
            state.mark_field(pid, name, product.get("row_created_timestamp"))

    if "categorie" in plan.fields:
        await client.set_product_fields(pid, {"product_group_id": plan.group_id})
        done("categorie")
    if "nutrition" in plan.fields:
        nutrition = plan.nutrition
        fields: dict[str, object] = {NUTRI_BY_KEY[k]: _num(v) for k, v in nutrition["valeurs"].items() if k in NUTRI_BY_KEY}
        if not (product.get("userfields") or {}).get(NUTRI_WEIGHT):
            fields[NUTRI_WEIGHT] = _num(nutrition["poids"])
        fields["nutri_source"] = f"Open Food Facts {code} (automatique{', ml comptés 1 g' if nutrition['approximatif'] else ''})"
        await client.set_userfields(pid, fields)
        await client.set_product_calories(pid, nutrition["calories"])
        done("nutrition")
    if "photo" in plan.fields and sources is not None:
        try:
            data, ext = await sources.fetch_image(plan.image_url)
        except _OFF_FAILURES:
            cache.drop_image(code)  # jamais retéléchargée tant que la fiche est en cache
            return
        name = await client.upload_product_picture(_picture_name(pid, code, ext), data)
        await client.set_product_fields(pid, {"picture_file_name": name})
        done("photo")


async def run_packaging(
    client: GrocyClient,
    sources: FoodSources | None,
    cache: OffCache,
    *,
    dry_run: bool = False,
    audit: AuditLog | None = None,
    state: JobState | None = None,
    enrich: bool = False,
    rules: list[list[str]] | None = None,
) -> dict:
    """Écrit les conversions certaines ; ne touche ni au stock, ni aux noms, ni aux conversions existantes (C6).

    Avec ``enrich``, complète aussi les champs vides (catégorie, nutrition, photo) depuis Open Food Facts (E1 à E9).
    """
    products, units, conversions, codes = await load_catalog(client)
    account = (await client.current_user()).id if audit is not None and not dry_run else None
    groups = (await client.product_groups() or []) if enrich and rules else []
    lines, written, lookups = [], 0, 0
    for product in products:
        pid, name = int(product["id"]), clean_name(product.get("name"))
        codes_of = codes.get(pid, [])
        if state is not None:
            state.renew(product)
        filled = state.filled.get(pid, set()) if state is not None else set()
        wants = enrich and bool(codes_of) and bool(missing_fields(product, filled))
        retired = state is not None and pid in state.done and not own_conversions(pid, conversions)
        failed = False
        if sources is not None and codes_of:
            pack_lookup = not retired and needs_lookup(product, conversions, codes_of, cache)
            enrich_lookup = wants and not cache.record(codes_of[0])[0]
            if pack_lookup or enrich_lookup:
                if lookups >= MAX_LOOKUPS_PER_PASS:
                    lines.append({"produit": name, "statut": "reporte", "source": None, "contenance": None})
                    continue
                if lookups and LOOKUP_PAUSE:
                    await asyncio.sleep(LOOKUP_PAUSE)
                lookups += 1
                if enrich_lookup:
                    try:
                        await _off_record(sources, cache, codes_of[0])
                    except _Failed:
                        failed = True  # réessayé au prochain passage, sans seconde requête dans celui-ci
        if retired:
            # déjà traité et plus aucune conversion : retirée ou écartée par une personne, jamais recréée
            line = {"produit": name, "statut": "retire", "source": None, "contenance": None}
        else:
            line, created = await _package(client, None if failed else sources, cache, product, units, conversions,
                                           codes_of, dry_run, audit, state, account)
            written += line["statut"] == "ecrit"
            conversions = conversions + [{"product_id": pid, "from_qu_id": a, "to_qu_id": b, "factor": f} for a, b, f in created]
        if wants:
            plan = plan_enrichment(product, cache.record(codes_of[0])[1], units, conversions, groups, rules or [], filled)
            if plan.fields:
                done: list[str] = []
                if dry_run:
                    done = list(plan.fields)
                else:
                    try:
                        await _enrich(client, sources, cache, product, codes_of[0], plan, state, done)
                    except GrocyError as error:
                        if audit is not None:
                            audit.write("enrichissement_partiel", compte_id=account, produit_id=pid, champs=done)
                        if not isinstance(error, GrocyRequestError):
                            raise
                        line["enrichissement"] = "erreur"
                        done = []
                    else:
                        if done and audit is not None:
                            audit.write("enrichissement", compte_id=account, produit_id=pid, champs=done,
                                        code_barres=codes_of[0])
                if done:
                    line["enrichi"] = done
        lines.append(line)
    return {"ecrits": written, "produits": lines}


async def _package(
    client: GrocyClient, sources: FoodSources | None, cache: OffCache, product: dict, units: dict[int, str],
    conversions: list[dict], codes_of: list[str], dry_run: bool, audit: AuditLog | None, state: JobState | None,
    account: int | None,
) -> tuple[dict, list[list]]:
    pid, name = int(product["id"]), clean_name(product.get("name"))
    decision = await decide(product, units, conversions, codes_of, sources, cache, ours=state is not None and pid in state.done)
    status = decision.status
    created: list[list] = []
    if status == "a_ecrire" and not dry_run:
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
                    state.mark(pid, product.get("row_created_timestamp"))
                if audit is not None:
                    audit.write("conditionnement_partiel", compte_id=account, produit_id=pid, conversions=created)
            if not isinstance(error, GrocyRequestError):
                raise  # clé refusée ou Grocy injoignable : tout le passage s'arrête
            status = "erreur"  # les autres produits continuent
        else:
            status = "ecrit"
            if state is not None:
                state.mark(pid, product.get("row_created_timestamp"))
            if audit is not None:
                audit.write(
                    "conditionnement", compte_id=account, produit_id=pid, source=decision.source,
                    contenance=describe(decision.content),
                    conversions=[[a, b, f] for a, b, f in decision.conversions], unite_achat=decision.purchase_unit,
                )
    line = {"produit": name, "statut": status, "source": decision.source, "contenance": describe(decision.content)}
    return line, created


__all__ = ["AMBIGUOUS", "OffCache", "decide", "describe", "load_catalog", "JobState", "needs_lookup", "run_packaging"]
