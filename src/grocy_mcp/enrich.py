"""Enrichissement des fiches produits : photo, apports nutritionnels et catégorie depuis Open Food Facts.

Décisions pures, sans réseau : seuls les champs vides sont remplis, jamais un champ qu'une personne a vidé après le
passage, et rien en cas de doute (spec enrichissement E1 à E6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from grocy_mcp.nutrition import MAX_UNIT_GRAMS, MIN_UNIT_GRAMS, check_values
from grocy_mcp.packaging import metric_kind, own_conversions

FIELDS = ("categorie", "nutrition", "photo")
IMAGE_HOST = "images.openfoodfacts.org"
MAX_IMAGE_BYTES = 1_000_000
NUTRI_BY_KEY = {
    "kcal": "nutri_kcal_100g", "proteines": "nutri_proteines_100g", "glucides": "nutri_glucides_100g",
    "sucres": "nutri_sucres_100g", "lipides": "nutri_lipides_100g", "ags": "nutri_ags_100g",
    "fibres": "nutri_fibres_100g", "sel": "nutri_sel_100g",
}
NUTRI_VALUES = tuple(NUTRI_BY_KEY.values())
NUTRI_WEIGHT, NUTRI_NON_FOOD = "nutri_poids_unite_g", "nutri_non_alimentaire"
TOLERANCE = 0.01


def _filled(value: object) -> bool:
    return value not in (None, "", 0, "0")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _userfields(product: dict) -> dict:
    fields = product.get("userfields")
    return fields if isinstance(fields, dict) else {}


def has_nutrition(product: dict) -> bool:
    fields = _userfields(product)
    if _filled(fields.get(NUTRI_NON_FOOD)):  # produit non alimentaire : rien à remplir
        return True
    calories = _number(product.get("calories"))
    return bool(calories) or any(_filled(fields.get(name)) for name in NUTRI_VALUES)


def missing_fields(product: dict, done: set[str]) -> list[str]:
    """Champs vides que le passage n'a jamais remplis pour ce produit (E1, E2)."""
    empty = {
        "categorie": not _filled(product.get("product_group_id")),
        "nutrition": not has_nutrition(product),
        "photo": not _filled(product.get("picture_file_name")),
    }
    return [name for name in FIELDS if empty[name] and name not in done]


def _in_bounds(grams: float) -> bool:
    return MIN_UNIT_GRAMS <= grams <= MAX_UNIT_GRAMS


def unit_weight(product: dict, units: dict[int, str], conversions: list[dict]) -> tuple[float, bool] | None:
    """Poids d'une unité de stock en grammes (ml comptés 1 g, approximatif), ou None s'il n'est pas certain (E4)."""
    typed = _number(_userfields(product).get(NUTRI_WEIGHT))
    if typed is not None:
        return (typed, False) if _in_bounds(typed) else None
    stock = int(product["qu_id_stock"])
    kind = metric_kind(units.get(stock, ""))
    if kind is not None:
        grams = kind[1]
        return (grams, kind[0] == "volume") if _in_bounds(grams) else None
    found: list[tuple[float, bool]] = []
    for row in own_conversions(int(product["id"]), conversions):
        if int(row["from_qu_id"]) != stock:
            continue
        target = metric_kind(units.get(int(row["to_qu_id"]), ""))
        factor = _number(row.get("factor"))
        if target is None or factor is None or factor <= 0:
            continue
        found.append((round(factor * target[1], 3), target[0] == "volume"))
    if not found:
        return None
    grams, approx = found[0]
    if any(abs(g - grams) > TOLERANCE * grams for g, _ in found[1:]):  # conversions discordantes : aucun choix
        return None
    grams = int(grams) if grams == int(grams) else grams
    return (grams, approx or any(a for _, a in found)) if _in_bounds(grams) else None


def pick_group(categories: list[str], rules: list[list[str]], groups: list[dict]) -> int | None:
    """Première règle (la plus précise) dont l'étiquette Open Food Facts figure sur la fiche (E6)."""
    tags = {str(tag) for tag in categories or [] if isinstance(tag, str)}
    for rule in rules or []:
        if not (isinstance(rule, list | tuple) and len(rule) == 2) or rule[0] not in tags:
            continue
        code = str(rule[1]).strip()
        matches = [int(g["id"]) for g in groups if str(g.get("name", "")) == code
                   or str(g.get("name", "")).startswith(code + " ")]
        return matches[0] if len(matches) == 1 else None
    return None


def valid_image_url(url: object) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == IMAGE_HOST and port is None and "@" not in parts.netloc


def image_kind(data: bytes) -> str | None:
    """Extension d'après la signature du fichier : JPEG, PNG ou WebP seulement."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


@dataclass
class Plan:
    fields: list[str] = field(default_factory=list)
    group_id: int | None = None
    nutrition: dict | None = None
    image_url: str | None = None
    reasons: dict[str, str] = field(default_factory=dict)


def plan_enrichment(
    product: dict, record: dict | None, units: dict[int, str], conversions: list[dict],
    groups: list[dict], rules: list[list[str]], done: set[str],
) -> Plan:
    plan = Plan()
    if not isinstance(record, dict):
        return plan
    for name in missing_fields(product, done):
        if name == "categorie":
            cats = record.get("cats")
            plan.group_id = pick_group(cats if isinstance(cats, list) else [], rules, groups)
            if plan.group_id is None:
                plan.reasons[name] = "aucune règle"
                continue
        elif name == "photo":
            if not valid_image_url(record.get("img")):
                plan.reasons[name] = "pas de photo"
                continue
            plan.image_url = record["img"]
        else:
            values = record.get("v") if isinstance(record.get("v"), dict) else {}
            if "kcal" not in values or check_values(values):
                plan.reasons[name] = "valeurs absentes ou invraisemblables"
                continue
            weight = unit_weight(product, units, conversions)
            if weight is None:
                plan.reasons[name] = "poids inconnu"
                continue
            plan.nutrition = {"valeurs": values, "poids": weight[0], "approximatif": weight[1],
                              "calories": values["kcal"] * weight[0] / 100}
        plan.fields.append(name)
    return plan


__all__ = ["FIELDS", "MAX_IMAGE_BYTES", "NUTRI_BY_KEY", "NUTRI_WEIGHT", "Plan", "image_kind", "missing_fields", "pick_group", "plan_enrichment",
           "unit_weight", "valid_image_url"]
