"""Sources externes d'apports nutritionnels par code-barres : Open Food Facts, puis USDA (spec B §3).

Liste fermée d'hôtes, HTTPS, sans redirection, délai et taille de réponse plafonnés.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata

import httpx2

from grocy_mcp import __version__
from grocy_mcp.enrich import MAX_IMAGE_BYTES, image_kind, valid_image_url
from grocy_mcp.nutrition import weight_in_grams

_BARCODE = re.compile(r"[0-9]{8,14}")
_MULTIPACK = re.compile(r"\d\s*[x×*]\s*\d", re.I)
_NAME_WEIGHT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:kg|gr|g)\b", re.I)
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
OFF_RECORD_FIELDS = (
    "quantity,product_quantity,product_quantity_unit,nutriments,image_front_url,image_url,categories_tags"
)
MAX_CATEGORIES = 100
TIMEOUT = 10.0
MAX_BYTES = 1_000_000
USER_AGENT = f"grocy-mcp-server/{__version__} (+https://github.com/darkvadehors/grocy-mcp-server)"
_OFF_FIELDS = {
    "kcal": "energy-kcal_100g",
    "proteines": "proteins_100g",
    "glucides": "carbohydrates_100g",
    "sucres": "sugars_100g",
    "lipides": "fat_100g",
    "ags": "saturated-fat_100g",
    "fibres": "fiber_100g",
    "sel": "salt_100g",
}
# Identifiants de nutriments de FoodData Central (valeurs pour 100 g des produits de marque)
_USDA_FIELDS = {1008: "kcal", 1003: "proteines", 1005: "glucides", 2000: "sucres", 1004: "lipides", 1258: "ags", 1079: "fibres"}
_USDA_SODIUM_MG = 1093


class SourceError(Exception):
    """Source injoignable ou réponse refusée (redirection, taille, format)."""


class TooLarge(SourceError):
    """Réponse au-delà de la taille permise."""


class ImageRejected(SourceError):
    """Photo refusée pour de bon (adresse, absente, trop lourde, pas une image) ; une panne n'en est pas une."""


def _clean(value: object, limit: int = 120) -> str:
    text = "".join(ch for ch in str(value or "") if unicodedata.category(ch)[0] != "C")
    return " ".join(text.split())[:limit]


def _number(value: object) -> float | None:
    """Nombre fini, ou None (texte illisible, booléen, NaN et infini refusés : la base est participative)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value) if isinstance(value, int | float) else float(str(value).replace(",", "."))
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _compact(value: float) -> float | int:
    return int(value) if value == int(value) else value


def _off_values(product: dict) -> dict:
    """Valeurs pour 100 g d'une fiche Open Food Facts (énergie en kJ convertie)."""
    nutriments = product.get("nutriments") if isinstance(product.get("nutriments"), dict) else {}
    values = {}
    for key, field in _OFF_FIELDS.items():
        number = _number(nutriments.get(field))
        if number is not None:
            values[key] = _compact(number)
    if "kcal" not in values:  # énergie seulement en kJ sur beaucoup de fiches
        kj = _number(nutriments.get("energy-kj_100g")) or _number(nutriments.get("energy_100g"))
        if kj is not None:
            values["kcal"] = round(kj / 4.184)
    return values


def _off_quantity_text(product: dict) -> str | None:
    quantity = product.get("quantity")
    text = _clean(quantity, 60) if isinstance(quantity, str) else ""
    if text:
        return text
    amount = _number(product.get("product_quantity"))
    unit = _clean(product.get("product_quantity_unit"), 10) or "g"
    return f"{_compact(amount)} {unit}" if amount is not None and amount > 0 else None


def _metric_weight(text: object) -> tuple[float, bool] | None:
    """« 400 g », « 1,5 kg », « 13 oz/371 g » → poids ; un lot (« 6 x 33 cl ») n'est pas deviné."""
    text = str(text or "")
    if _MULTIPACK.search(text):
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|mg|gr|g|cl|dl|ml|l)\b", text, re.I)
    return weight_in_grams(_number(match.group(1)), match.group(2)) if match else None


def _off_weight(product: dict) -> tuple[float, bool] | None:
    if _MULTIPACK.search(str(product.get("quantity") or "")):  # « 4 x 125 g » : poids d'une unité inconnu
        return None
    amount = _number(product.get("product_quantity"))
    if amount is not None:
        return weight_in_grams(amount, product.get("product_quantity_unit") or "g")  # unité absente : grammes
    return _metric_weight(product.get("quantity"))


def _probable_pack(weight: tuple[float, bool] | None, product: dict) -> tuple[int, float] | None:
    """Contenance multiple entier de la portion (415 g = 10 × 41,5 g) : paquet de plusieurs unités probable."""
    serving = _number(product.get("serving_quantity"))
    if serving is None:
        match = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*g\b", str(product.get("serving_size") or ""), re.I)
        serving = _number(match.group(1)) if match else None
    if not weight or not serving or serving <= 0:
        return None
    count = weight[0] / serving
    if count < 2 or abs(count - round(count)) >= 0.02:
        return None
    # Une portion conseillée (riz : 62,5 g) ne fait pas un lot : il faut que le nom indique le poids d'une unité
    # (« KITKAT barre 41,5g ») et non le poids total (« Riz Noir 500 g »).
    in_name = [w for w in (_metric_weight(m.group(0)) for m in _NAME_WEIGHT.finditer(str(product.get("product_name") or ""))) if w]
    if any(abs(w[0] - serving) < 0.01 for w in in_name) and not any(abs(w[0] - weight[0]) < 0.01 for w in in_name):
        return int(round(count)), _compact(serving)
    return None


class FoodSources:
    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None, usda_key: str | None = None) -> None:
        self._transport = transport
        self._usda_key = usda_key

    async def _get_bytes(self, url: str, params: dict | None = None, limit: int = MAX_BYTES,
                         missing_ok: bool = True) -> bytes | None:
        async with httpx2.AsyncClient(
            transport=self._transport, timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        ) as http:
            try:
                async with http.stream("GET", url, params=params) as response:
                    if response.status_code == 404 and missing_ok:  # Open Food Facts : produit inconnu
                        return None
                    if response.status_code != 200:
                        raise SourceError(f"réponse {response.status_code}")
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > limit:
                            raise TooLarge("réponse trop volumineuse")
            except httpx2.HTTPError:
                raise SourceError("source injoignable") from None
        return body

    async def _get_json(self, url: str, params: dict | None = None) -> object:
        body = await self._get_bytes(url, params)
        if body is None:
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise SourceError("réponse illisible") from None

    async def off(self, code: str) -> dict | None:
        if not _BARCODE.fullmatch(code or ""):
            raise SourceError("code-barres invalide")
        data = await self._get_json(
            OFF_URL.format(code=code),
            # « quantity » est indispensable : sans lui, Open Food Facts omet product_quantity quand nutriments est demandé
            params={"fields": "product_name,quantity,product_quantity,product_quantity_unit,serving_size,serving_quantity,nutriments"},
        )
        product = data.get("product") if isinstance(data, dict) and data.get("status") == 1 else None
        if not isinstance(product, dict):
            return None
        values = _off_values(product)
        if not values:
            return None
        weight = _off_weight(product)
        return {
            "source": "Open Food Facts",
            "reference": code,
            "nom": _clean(product.get("product_name")),
            "valeurs": values,
            "poids": weight,
            "lot": _probable_pack(weight, product),
        }

    async def off_quantity(self, code: str) -> str | None:
        """Contenance déclarée sur Open Food Facts (« 250 g », « 6 x 27,5 cl »), ou None si inconnue."""
        if not _BARCODE.fullmatch(code or ""):
            raise SourceError("code-barres invalide")
        data = await self._get_json(OFF_URL.format(code=code), params={"fields": "quantity,product_quantity,product_quantity_unit"})
        product = data.get("product") if isinstance(data, dict) and data.get("status") == 1 else None
        return _off_quantity_text(product) if isinstance(product, dict) else None

    async def off_record(self, code: str) -> dict | None:
        """Fiche Open Food Facts réduite, en une requête : contenance, valeurs pour 100 g, photo, catégories.

        None si le produit est inconnu ; les champs illisibles sont vides, jamais devinés.
        """
        if not _BARCODE.fullmatch(code or ""):
            raise SourceError("code-barres invalide")
        data = await self._get_json(OFF_URL.format(code=code), params={"fields": OFF_RECORD_FIELDS})
        product = data.get("product") if isinstance(data, dict) and data.get("status") == 1 else None
        if not isinstance(product, dict):
            return None
        image = next((u for u in (product.get("image_front_url"), product.get("image_url")) if valid_image_url(u)), None)
        tags = product.get("categories_tags")
        cats = [_clean(t, 80) for t in tags if isinstance(t, str)][:MAX_CATEGORIES] if isinstance(tags, list) else []
        return {"q": _off_quantity_text(product), "v": _off_values(product), "img": image, "cats": [c for c in cats if c]}

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        """Photo d'Open Food Facts : hôte d'images seulement, sans redirection, 1 Mo au plus, JPEG, PNG ou WebP."""
        if not valid_image_url(url):
            raise ImageRejected("adresse d'image refusée")
        try:
            data = await self._get_bytes(url, limit=MAX_IMAGE_BYTES)
        except TooLarge:
            raise ImageRejected("image trop volumineuse") from None
        if data is None:
            raise ImageRejected("réponse 404 : photo absente")
        kind = image_kind(data)
        if kind is None:
            raise ImageRejected("ce n'est pas une image JPEG, PNG ou WebP")
        return data, kind

    async def usda(self, code: str) -> dict | None:
        if not self._usda_key:
            return None
        if not _BARCODE.fullmatch(code or ""):
            raise SourceError("code-barres invalide")
        data = await self._get_json(
            USDA_URL, params={"query": code, "dataType": "Branded", "pageSize": 5, "api_key": self._usda_key}
        )
        foods = data.get("foods") if isinstance(data, dict) else None
        food = next(
            (f for f in foods or [] if isinstance(f, dict) and str(f.get("gtinUpc", "")).lstrip("0") == code.lstrip("0")),
            None,
        )
        if food is None:
            return None
        values: dict[str, float | int] = {}
        for item in food.get("foodNutrients") or []:
            if not isinstance(item, dict):
                continue
            number = _number(item.get("value"))
            if number is None:
                continue
            nutrient = item.get("nutrientId")
            if not isinstance(nutrient, int) or isinstance(nutrient, bool):
                continue
            if nutrient in _USDA_FIELDS:
                values[_USDA_FIELDS[nutrient]] = _compact(number)
            elif nutrient == _USDA_SODIUM_MG:
                values["sel"] = round(number * 2.5 / 1000, 4)  # sel = sodium × 2,5
        if not values:
            return None
        weight = _metric_weight(str(food.get("packageWeight") or "").split("/")[-1])
        return {"source": "USDA", "reference": code, "nom": _clean(food.get("description")), "valeurs": values, "poids": weight}
