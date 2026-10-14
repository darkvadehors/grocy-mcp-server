"""Conditionnement des produits : contenance lue dans un texte et conversions d'unités à créer (spec conditionnement).

Fonctions pures, sans accès réseau : le passage automatique et les outils MCP s'appuient dessus.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Unités métriques reconnues dans Grocy (nom replié) → (dimension, valeur en g ou en mL)
_METRIC = {
    "milligramme": ("mass", 0.001),
    "gramme": ("mass", 1),
    "kilogramme": ("mass", 1000),
    "tonne": ("mass", 1_000_000),
    "millilitre": ("volume", 1),
    "centilitre": ("volume", 10),
    "decilitre": ("volume", 100),
    "litre": ("volume", 1000),
    "metre cube": ("volume", 1_000_000),
}
# Abréviation ou mot trouvé dans un nom de produit → nom de l'unité Grocy
_WRITTEN = {
    "mg": "Milligramme", "milligramme": "Milligramme", "milligrammes": "Milligramme",
    "g": "Gramme", "gr": "Gramme", "gramme": "Gramme", "grammes": "Gramme",
    "kg": "Kilogramme", "kilogramme": "Kilogramme", "kilogrammes": "Kilogramme",
    "ml": "Millilitre", "millilitre": "Millilitre", "millilitres": "Millilitre",
    "cl": "Centilitre", "centilitre": "Centilitre", "centilitres": "Centilitre",
    "dl": "Décilitre",
    "l": "Litre", "litre": "Litre", "litres": "Litre",
}
_UNIT = r"(milligrammes?|kilogrammes?|grammes?|millilitres?|centilitres?|litres?|kg|mg|gr|g|ml|cl|dl|l)"
_NUM = r"(\d+(?:[.,]\d+)?)"
_END = r"(?![^\W\d_])"  # l'unité n'est pas le début d'un mot (« 3 lots »)
_PACK = re.compile(rf"(?<![\d.,])(\d{{1,3}})\s*[x×*]\s*{_NUM}\s*{_UNIT}{_END}", re.I)
_SINGLE = re.compile(rf"(?<![\d.,]){_NUM}\s*{_UNIT}{_END}", re.I)
TOLERANCE = 0.01
PACKET_NAMES = ("Paquet", "Pièce")  # unités d'achat d'un produit vendu au poids ou au volume


class _Ambiguous:
    def __repr__(self) -> str:
        return "AMBIGUOUS"


AMBIGUOUS = _Ambiguous()  # plusieurs contenances discordantes : rien ne doit être écrit


@dataclass(frozen=True)
class Content:
    """count éléments de amount <unit> chacun (count > 1 : lot, « 6 x 27,5 cl »)."""

    count: int
    amount: float
    unit: str  # nom de l'unité Grocy (« Gramme », « Centilitre »…)

    def per_item(self) -> tuple[str, float]:
        kind, factor = metric_kind(self.unit)  # type: ignore[misc]
        return kind, self.amount * factor


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).casefold())
    text = "".join(c for c in text if not unicodedata.combining(c) and (unicodedata.category(c)[0] not in "CZ" or c == " "))
    return " ".join(text.split())


def clean_name(value: object, limit: int = 120) -> str:
    """Nom lisible pour un rapport : sans caractères de contrôle ni invisibles (U+FFFC d'un copier-coller iOS)."""
    text = "".join(c for c in str(value or "") if unicodedata.category(c)[0] != "C" and c != "\ufffc")
    return " ".join(text.split())[:limit]


def metric_kind(unit_name: str) -> tuple[str, float] | None:
    """(« mass » ou « volume », valeur d'une unité en g ou en mL), ou None pour une unité de comptage."""
    return _METRIC.get(fold(unit_name))


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _same(a: tuple[str, float], b: tuple[str, float]) -> bool:
    return a[0] == b[0] and abs(a[1] - b[1]) <= TOLERANCE * max(a[1], b[1])


def parse_content(text: str | None) -> Content | _Ambiguous | None:
    """Contenance écrite dans un nom (« 500 g », « 75 cl », « 6X27,5CL ») ; AMBIGUOUS si elle se contredit."""
    text = str(text or "")
    packs = []
    for m in _PACK.finditer(text):
        count, amount = int(m.group(1)), _number(m.group(2))
        if count >= 2 and amount > 0:
            packs.append(Content(count, amount, _WRITTEN[m.group(3).lower()]))
    rest = _PACK.sub(" ", text)
    singles = [Content(1, _number(m.group(1)), _WRITTEN[m.group(2).lower()]) for m in _SINGLE.finditer(rest)]
    singles = [s for s in singles if s.amount > 0]
    if packs:
        pack = packs[0]
        kind, each = pack.per_item()
        if any(not _same(p.per_item(), (kind, each)) or p.count != pack.count for p in packs[1:]):
            return AMBIGUOUS
        # un autre poids n'est admis que s'il vaut une unité ou le total du lot (« 6X27,5CL … 1650 ml »)
        if any(not (_same(s.per_item(), (kind, each)) or _same(s.per_item(), (kind, each * pack.count))) for s in singles):
            return AMBIGUOUS
        return pack
    if not singles:
        return None
    first = singles[0]
    if any(not _same(s.per_item(), first.per_item()) for s in singles[1:]):
        return AMBIGUOUS
    return first


@dataclass
class Decision:
    status: str  # a_ecrire | deja | lot | contradictoire | incompatible | inconnu | unite_absente
    conversions: list[tuple[int, int, float]] = field(default_factory=list)  # (de, vers, facteur) : 1 de = facteur vers
    purchase_unit: int | None = None  # unité d'achat et de prix à poser (produit vendu au poids ou au volume)
    source: str | None = None
    content: Content | None = None


def _unit_id(units: dict[int, str], name: str) -> int | None:
    wanted = fold(name)
    return next((uid for uid, label in units.items() if fold(label) == wanted), None)


def resolve_unit(units: dict[int, str], text: str | None, plurals: dict[int, str] | None = None) -> int | None:
    """Unité Grocy désignée par son nom, son pluriel ou une abréviation (« g », « cL », « bouteilles »)."""
    wanted = fold(text or "")
    if not wanted:
        return None
    if wanted in _WRITTEN:
        return _unit_id(units, _WRITTEN[wanted])
    found = _unit_id(units, wanted)
    if found is None and plurals:
        found = next((uid for uid, label in plurals.items() if label and fold(label) == wanted and uid in units), None)
    return found


def own_conversions(product_id: int, conversions: list[dict]) -> list[dict]:
    return [c for c in conversions if c.get("product_id") not in (None, "") and int(c["product_id"]) == int(product_id)]


def plan_product(
    product: dict, units: dict[int, str], conversions: list[dict], candidates: list[tuple[str, object]]
) -> Decision:
    """Conversions à créer pour un produit ; rien dès qu'un doute existe (spec conditionnement C3 à C5)."""
    if own_conversions(int(product["id"]), conversions):
        return Decision("deja")
    found = [(source, content) for source, content in candidates if content is not None]
    if not found:
        return Decision("inconnu")
    if any(content is AMBIGUOUS for _, content in found):
        return Decision("contradictoire")
    source, first = found[0]
    assert isinstance(first, Content)
    for _, other in found[1:]:
        assert isinstance(other, Content)
        if other.count != first.count or not _same(other.per_item(), first.per_item()):
            return Decision("contradictoire")
    if first.count > 1:
        return Decision("lot", source=source, content=first)
    stock_id = int(product["qu_id_stock"])
    stock_kind = metric_kind(units.get(stock_id, ""))
    if stock_kind is None:
        target = _unit_id(units, first.unit)
        if target is None:
            return Decision("unite_absente", source=source, content=first)
        return Decision("a_ecrire", [(stock_id, target, first.amount)], source=source, content=first)
    kind, each = first.per_item()
    if kind != stock_kind[0]:
        return Decision("incompatible", source=source, content=first)
    factor = round(each / stock_kind[1], 6)
    packets = [uid for uid in (_unit_id(units, name) for name in PACKET_NAMES) if uid is not None and uid != stock_id]
    if not packets:
        return Decision("unite_absente", source=source, content=first)
    packet = _unit_id(units, PACKET_NAMES[0])
    purchase = int(product.get("qu_id_purchase") or stock_id)
    buy = packet if packet is not None and purchase == stock_id else None
    return Decision("a_ecrire", [(uid, stock_id, factor) for uid in packets], purchase_unit=buy, source=source, content=first)
