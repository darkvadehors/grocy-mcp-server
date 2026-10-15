"""Prix des courses : décisions pures, sans réseau (spec prix P1 à P5).

Magasin d'un ticket, remises, contrôle du total, propositions de produits, quantité en unité de stock, prix au kg ou
au litre, dernier prix par magasin, répartition d'une liste entre magasins, hausses de prix. Rien n'est deviné : une
quantité ou un poids incertain donne None.
"""

from __future__ import annotations

import itertools
import re
import unicodedata

from grocy_mcp.enrich import unit_weight
from grocy_mcp.packaging import metric_kind

MAX_STORES = 10  # magasins essayés pour la répartition (les plus fournis en prix)
RISE = 0.10
_WORD = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def _cents(value: float) -> float:
    return round(value + 0.0, 2)


def match_store(locations: list[dict], name: str) -> dict | None:
    """Magasin Grocy dont le nom est celui du ticket ou son début suivi d'une espace ; le plus long l'emporte."""
    wanted = fold(name)
    if not wanted:
        return None
    found = [loc for loc in locations if (n := fold(str(loc.get("name", "")))) and (wanted == n or wanted.startswith(n + " "))]
    return max(found, key=lambda loc: len(fold(str(loc["name"]))), default=None)


def apply_discounts(lines: list[dict]) -> tuple[list[dict], list[str]]:
    """Retranche chaque remise (prix négatif) de l'article qui la précède ; sinon la signale sans l'appliquer."""
    articles: list[dict] = []
    warnings: list[str] = []
    for line in lines:
        price = float(line["prix"])
        if price >= 0:
            articles.append({**line, "remise": 0})
            continue
        if not articles:
            warnings.append(f"ligne {line['ligne']} : remise sans article avant elle, non appliquée")
            continue
        target = articles[-1]
        if target["prix"] + price < 0:
            warnings.append(f"ligne {line['ligne']} : remise supérieure à l'article précédent, non appliquée")
            continue
        target["prix"] = _cents(target["prix"] + price)
        target["remise"] = _cents(target["remise"] - price)
    return articles, warnings


def check_total(lines: list[dict], total: float) -> float:
    """Écart entre la somme des lignes (remises comprises) et le total payé, en euros."""
    return _cents(sum(float(line["prix"]) for line in lines) - float(total))


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(fold(text)) if len(w) >= 3 and not w.isdigit()}


def suggest_products(label: str, products: list[dict], limit: int = 3) -> list[dict]:
    """Produits partageant des mots (ou débuts de mots, abréviations de ticket) avec le libellé ; jamais d'office."""
    wanted = _words(label)
    scored = []
    for product in products:
        names = _words(str(product.get("name", "")))
        score = sum(1 for w in wanted if any(n.startswith(w) or w.startswith(n) for n in names))
        if score:
            scored.append((-score, fold(str(product["name"])), product))
    return [p for *_, p in sorted(scored, key=lambda s: (s[0], s[1]))[:limit]]


def _factor(conversions: list[dict], product_id: int, from_qu: int, to_qu: int) -> float | None:
    """Facteur de conversion propre au produit, sinon général ; dans un sens ou dans l'autre."""
    for scope in (product_id, None):
        for row in conversions:
            pid = row.get("product_id")
            if (None if pid is None else int(pid)) != scope:
                continue
            factor = float(row.get("factor") or 0)
            if factor <= 0:
                continue
            if int(row["from_qu_id"]) == from_qu and int(row["to_qu_id"]) == to_qu:
                return factor
            if int(row["from_qu_id"]) == to_qu and int(row["to_qu_id"]) == from_qu:
                return 1 / factor
    return None


def stock_amount(line: dict, product: dict, units: dict[int, str], conversions: list[dict]) -> float | None:
    """Quantité d'une ligne de ticket en unité de stock, ou None si elle n'est pas certaine."""
    if line.get("poids_kg"):
        weight = unit_weight(product, units, conversions)
        if weight is None or weight[1]:  # poids d'une unité inconnu, ou liquide compté 1 g/ml : rien d'inventé
            return None
        return round(float(line["poids_kg"]) * 1000 / weight[0], 4)
    quantity = float(line.get("quantite") or 1)
    stock = int(product["qu_id_stock"])
    purchase = int(product.get("qu_id_purchase") or stock)
    if metric_kind(units.get(purchase, "")) is not None:  # vendu au poids ou au volume : sans poids, rien de deviné
        return None
    if purchase == stock:
        return quantity
    factor = _factor(conversions, int(product["id"]), purchase, stock)
    return None if factor is None else round(quantity * factor, 4)


def normalized_price(price: float, product: dict, units: dict[int, str], conversions: list[dict]) -> dict:
    """Prix au kg (au litre pour un liquide), sinon par unité de stock."""
    weight = unit_weight(product, units, conversions)
    if weight is None:
        return {"valeur": _cents(price), "unite": units.get(int(product["qu_id_stock"]), "unité")}
    return {"valeur": _cents(price * 1000 / weight[0]), "unite": "l" if weight[1] else "kg"}


def latest_by_store(rows: list[dict]) -> dict[int, dict]:
    """Dernier prix connu par magasin (une ligne sans magasin est ignorée ; à date égale, la dernière lue)."""
    latest: dict[int, dict] = {}
    for row in rows:
        if row.get("location_id") is None:
            continue
        loc = int(row["location_id"])
        if loc not in latest or str(row["date"]) >= str(latest[loc]["date"]):
            latest[loc] = row
    return latest


def best_split(items: dict[str, dict[int, float]], max_stores: int) -> dict:
    """Combinaison d'au plus max_stores magasins couvrant le plus d'articles, puis la moins chère."""
    counts: dict[int, int] = {}
    for prices in items.values():
        for loc in prices:
            counts[loc] = counts.get(loc, 0) + 1
    stores = sorted(counts, key=lambda loc: (-counts[loc], loc))[:MAX_STORES]
    best: tuple | None = None
    for size in range(1, max_stores + 1):
        for combo in itertools.combinations(stores, size):
            assignment = {}
            total = 0.0
            for name, prices in items.items():
                offers = [(prices[loc], loc) for loc in combo if loc in prices]
                if offers:
                    price, loc = min(offers)
                    assignment[name] = loc
                    total += price
            used = sorted(set(assignment.values()))
            key = (-len(assignment), _cents(total), len(used), used)
            if best is None or key < best[0]:
                best = (key, assignment, used, total)
    if best is None:
        return {"magasins": [], "affectation": {}, "total": 0, "non_couverts": sorted(items), "reference": None}
    _, assignment, used, total = best
    covered = list(assignment)
    singles = [
        (_cents(sum(items[name][loc] for name in covered)), loc)
        for loc in stores
        if all(loc in items[name] for name in covered)
    ]
    reference = None
    if len(used) > 1 and singles:
        cost, loc = min(singles)
        reference = {"magasin": loc, "total": cost}
    return {
        "magasins": used,
        "affectation": assignment,
        "total": _cents(total),
        "non_couverts": [name for name in items if name not in assignment],
        "reference": reference,
    }


def price_rises(rows: list[dict], threshold: float = RISE) -> list[dict]:
    """Hausses de plus de threshold entre les deux derniers prix d'un même produit dans un même magasin."""
    groups: dict[tuple[int, int], list[dict]] = {}
    for row in rows:
        if row.get("location_id") is None:
            continue
        groups.setdefault((int(row["product_id"]), int(row["location_id"])), []).append(row)
    rises = []
    for (pid, loc), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        before, after = sorted(group, key=lambda r: str(r["date"]))[-2:]
        old, new = float(before["prix"]), float(after["prix"])
        if old > 0 and new > old * (1 + threshold):
            rises.append({"product_id": pid, "location_id": loc, "avant": old, "apres": new,
                          "hausse_pct": round((new / old - 1) * 100, 1), "date": after["date"]})
    return rises
