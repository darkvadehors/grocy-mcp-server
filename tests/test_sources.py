import json

import httpx2
import pytest

from grocy_mcp.sources import FoodSources, SourceError

pytestmark = pytest.mark.anyio

OFF_PRODUCT = {
    "status": 1,
    "product": {
        "product_name": "Pâtes Penne",
        "product_quantity": "500",
        "product_quantity_unit": "g",
        "nutriments": {
            "energy-kcal_100g": 359, "proteins_100g": 12.5, "carbohydrates_100g": 71, "sugars_100g": 3.5,
            "fat_100g": 1.5, "saturated-fat_100g": 0.3, "fiber_100g": 3, "salt_100g": 0.01,
        },
    },
}


def transport(routes, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response(request) if callable(response) else response
        return httpx2.Response(404)
    return httpx2.MockTransport(handler)


async def test_open_food_facts():
    """N2 : valeurs pour 100 g et poids lus, User-Agent du projet."""
    seen = []
    t = transport({"https://world.openfoodfacts.org/api/v2/product/3017620422003.json": httpx2.Response(200, json=OFF_PRODUCT)}, seen)
    found = await FoodSources(transport=t).off("3017620422003")
    assert found["source"] == "Open Food Facts" and found["nom"] == "Pâtes Penne"
    assert found["valeurs"] == {"kcal": 359, "proteines": 12.5, "glucides": 71, "sucres": 3.5, "lipides": 1.5, "ags": 0.3, "fibres": 3, "sel": 0.01}
    assert found["poids"] == (500, False)
    assert "fields=" in seen[0]


@pytest.mark.parametrize(("qty", "unit", "poids"), [("1,5", "kg", (1500, False)), ("33", "cl", (330, True)), (None, None, None), ("abc", "g", None)])
async def test_open_food_facts_poids(qty, unit, poids):
    product = json.loads(json.dumps(OFF_PRODUCT))
    product["product"]["product_quantity"], product["product"]["product_quantity_unit"] = qty, unit
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=product)})
    assert (await FoodSources(transport=t).off("3017620422003"))["poids"] == poids


async def test_open_food_facts_absent_ou_sans_valeurs():
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json={"status": 0})})
    assert await FoodSources(transport=t).off("3017620422003") is None
    empty = {"status": 1, "product": {"product_name": "X", "nutriments": {}}}
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=empty)})
    assert await FoodSources(transport=t).off("3017620422003") is None


async def test_usda_en_secours():
    usda = {"foods": [{"gtinUpc": "009800895007", "description": "NUTELLA", "packageWeight": "13 oz/371 g",
                       "foodNutrients": [{"nutrientId": 1008, "value": 541}, {"nutrientId": 1003, "value": 5.4},
                                         {"nutrientId": 1005, "value": 59.5}, {"nutrientId": 2000, "value": 56.8},
                                         {"nutrientId": 1004, "value": 31}, {"nutrientId": 1258, "value": 10.8},
                                         {"nutrientId": 1079, "value": 2.7}, {"nutrientId": 1093, "value": 41}]}]}
    seen = []
    t = transport({"https://api.nal.usda.gov/fdc/v1/foods/search": httpx2.Response(200, json=usda)}, seen)
    found = await FoodSources(transport=t, usda_key="cle-usda").usda("009800895007")
    assert found["source"] == "USDA" and found["valeurs"]["kcal"] == 541 and found["valeurs"]["sel"] == pytest.approx(0.1025)
    assert found["poids"] == (371, False)
    assert await FoodSources(transport=t).usda("009800895007") is None  # sans clé : USDA ignoré


async def test_liste_fermee_redirection_taille():
    """N7 : redirection refusée, réponse trop grosse refusée, code-barres invalide refusé (aucune requête)."""
    seen = []
    redirect = transport({"https://world.openfoodfacts.org/": httpx2.Response(302, headers={"Location": "https://evil.example/x"})}, seen)
    with pytest.raises(SourceError):
        await FoodSources(transport=redirect).off("3017620422003")
    big = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, content=b"{" + b" " * 1_100_000 + b"}")})
    with pytest.raises(SourceError):
        await FoodSources(transport=big).off("3017620422003")
    seen.clear()
    with pytest.raises(SourceError):
        await FoodSources(transport=redirect).off("../../evil")
    assert seen == []


async def test_reponse_hostile():
    hostile = {"status": 1, "product": {"product_name": "x" * 500 + "\n\x1b[31m", "nutriments": {"energy-kcal_100g": "beaucoup", "proteins_100g": 5}}}
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=hostile)})
    found = await FoodSources(transport=t).off("3017620422003")
    assert found["valeurs"] == {"proteines": 5} and len(found["nom"]) <= 120 and "\n" not in found["nom"]
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, content=b"pas du json")})
    with pytest.raises(SourceError):
        await FoodSources(transport=t).off("3017620422003")


async def test_produit_inconnu_404():
    """Open Food Facts répond 404 (status 0) pour un code inconnu : introuvable, pas une erreur."""
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(404, json={"status": 0, "status_verbose": "product not found"})})
    assert await FoodSources(transport=t).off("3561863419264") is None


@pytest.mark.parametrize(
    ("fields", "poids"),
    [
        ({"product_quantity": 400, "product_quantity_unit": None, "quantity": "400 g"}, (400, False)),  # unité absente : g
        ({"product_quantity": None, "quantity": "1,5 kg"}, (1500, False)),
        ({"product_quantity": None, "quantity": "6 x 33 cl"}, None),  # lot : non deviné
    ],
)
async def test_poids_sans_unite(fields, poids):
    product = json.loads(json.dumps(OFF_PRODUCT))
    product["product"].pop("product_quantity_unit")
    product["product"].update(fields)
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=product)})
    assert (await FoodSources(transport=t).off("3017620422003"))["poids"] == poids


async def test_r_c1_nan_infinity_et_usda_piege():
    """Revue C1 : NaN / Infinity / nutrientId non entier ne font pas planter ; valeurs ignorées."""
    body = b'{"status": 1, "product": {"product_name": "X", "nutriments": {"energy-kcal_100g": NaN, "proteins_100g": Infinity, "fat_100g": "nan", "carbohydrates_100g": 12}}}'
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, content=body)})
    found = await FoodSources(transport=t).off("3017620422003")
    assert found["valeurs"] == {"glucides": 12}
    usda = {"foods": [{"gtinUpc": "009800895007", "foodNutrients": [{"nutrientId": [1008], "value": 5}, {"nutrientId": 1008, "value": "inf"}, {"nutrientId": 1003, "value": 2}]}]}
    t = transport({"https://api.nal.usda.gov/": httpx2.Response(200, json=usda)})
    assert (await FoodSources(transport=t, usda_key="k").usda("009800895007"))["valeurs"] == {"proteines": 2}


async def test_r_i1_kj_converti_en_kcal():
    """Revue I1 : fiche avec l'énergie en kJ seulement → kcal = kJ / 4,184."""
    product = {"status": 1, "product": {"product_name": "X", "nutriments": {"energy-kj_100g": 1506, "proteins_100g": 10}}}
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=product)})
    assert (await FoodSources(transport=t).off("3017620422003"))["valeurs"]["kcal"] == 360


async def test_r_c2_lot_jamais_devine_meme_avec_product_quantity():
    product = json.loads(json.dumps(OFF_PRODUCT))
    product["product"].update({"product_quantity": 500, "quantity": "4 x 125 g"})
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=product)})
    assert (await FoodSources(transport=t).off("3017620422003"))["poids"] is None
    product["product"].update({"product_quantity": None, "quantity": "500 gr"})
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=product)})
    assert (await FoodSources(transport=t).off("3017620422003"))["poids"] == (500, False)


@pytest.mark.parametrize(
    ("name", "quantity", "serving", "lot"),
    [
        ("KITKAT barre chocolatée 41,5g", 415, {"serving_quantity": 41.5}, (10, 41.5)),
        ("KITKAT barre 41.5 g", 415, {"serving_size": "41.5 g ((41.5 g))"}, (10, 41.5)),
        ("Yaourt 125g", 500, {"serving_quantity": 125}, (4, 125)),
        ("Riz Noir 500 g", 500, {"serving_quantity": 62.5}, None),  # portion conseillée, pas un lot
        ("Riz Noir", 500, {"serving_quantity": 62.5}, None),
        ("Biscuits 30 g", 400, {"serving_quantity": 30}, None),  # pas un multiple entier
        ("Pâtes 100 g", 100, {"serving_quantity": 100}, None),
    ],
)
async def test_lot_probable_detecte(name, quantity, serving, lot):
    """Lot probable seulement si le nom indique le poids d'une portion (KitKat : « 41,5g », 415 g = 10 × 41,5 g)."""
    product = json.loads(json.dumps(OFF_PRODUCT))
    product["product"].update(
        {"product_name": name, "product_quantity": quantity, "product_quantity_unit": "g", "quantity": f"{quantity} g", **serving}
    )
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=product)})
    assert (await FoodSources(transport=t).off("3017620422003")).get("lot") == lot


# --- enrichissement : une seule requête par fiche, photo contrôlée ------------------------------------------
IMG = "https://images.openfoodfacts.org/images/products/301/762/042/2003/front_fr.12.400.jpg"
OFF_FULL = {"status": 1, "product": {**OFF_PRODUCT["product"], "quantity": "500 g", "image_front_url": IMG,
                                     "categories_tags": ["en:plant-based-foods", "en:pastas"]}}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


async def test_off_record_une_requete():
    seen = []
    t = transport({"https://world.openfoodfacts.org/api/v2/product/3017620422003.json": httpx2.Response(200, json=OFF_FULL)}, seen)
    record = await FoodSources(transport=t).off_record("3017620422003")
    assert record["q"] == "500 g" and record["v"]["kcal"] == 359 and record["img"] == IMG
    assert record["cats"] == ["en:plant-based-foods", "en:pastas"] and len(seen) == 1
    assert "image_front_url" in seen[0] and "categories_tags" in seen[0]


async def test_off_record_absent_et_fiche_piegee():
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(404, json={"status": 0})})
    assert await FoodSources(transport=t).off_record("3017620422003") is None
    hostile = {"status": 1, "product": {"quantity": ["x"], "nutriments": "rien", "image_front_url": 42,
                                         "image_url": "https://evil.test/a.jpg", "categories_tags": "en:pastas"}}
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=hostile)})
    record = await FoodSources(transport=t).off_record("3017620422003")
    assert record == {"q": None, "v": {}, "img": None, "cats": []}
    with pytest.raises(SourceError):
        await FoodSources(transport=t).off_record("abc")


async def test_off_record_image_de_secours_et_kj():
    product = {"status": 1, "product": {"image_url": IMG, "nutriments": {"energy-kj_100g": 1500},
                                         "categories_tags": ["en:a"] * 300}}
    t = transport({"https://world.openfoodfacts.org/": httpx2.Response(200, json=product)})
    record = await FoodSources(transport=t).off_record("3017620422003")
    assert record["img"] == IMG and record["v"] == {"kcal": 359} and len(record["cats"]) == 100


async def test_fetch_image():
    t = transport({IMG: httpx2.Response(200, content=JPEG)})
    assert await FoodSources(transport=t).fetch_image(IMG) == (JPEG, "jpg")


@pytest.mark.parametrize(
    ("url", "response", "error"),
    [
        ("https://evil.test/a.jpg", httpx2.Response(200, content=JPEG), "adresse"),
        (IMG, httpx2.Response(302, headers={"location": "https://evil.test/a.jpg"}), "réponse"),
        (IMG, httpx2.Response(200, content=b"<svg onload=alert(1)>"), "image"),
        (IMG, httpx2.Response(200, content=b"\xff\xd8\xff" + b"0" * 1_000_001), "volumineuse"),
        (IMG, httpx2.Response(404), "réponse"),
    ],
)
async def test_fetch_image_refus(url, response, error):
    t = transport({url: response, IMG: response})
    with pytest.raises(SourceError, match=error):
        await FoodSources(transport=t).fetch_image(url)


def _price(price, day, country="FR", city="Nancy", name="Carrefour Express"):
    return {"price": price, "currency": "EUR", "date": day, "price_per": None,
            "location": {"osm_address_country_code": country, "osm_address_city": city, "osm_name": name}}


async def test_open_prices_keeps_recent_french_prices():
    body = {"items": [_price(4.45, "2026-10-06"), _price(3.9, "2026-09-01", country="BE"), _price("x", "2026-09-01"),
                      _price(4.1, "2024-01-01"), _price(-1, "2026-09-02"), {"price": 5, "currency": "USD", "date": "2026-10-01"},
                      _price(4.2, "2026-08-01", name="Lidl <b>ignore</b>")]}
    seen = []
    t = transport({"https://prices.openfoodfacts.org/api/v1/prices": httpx2.Response(200, json=body)}, seen)
    found = await FoodSources(transport=t).open_prices("3017620422003", today="2026-10-15")
    assert [(p["prix"], p["date"]) for p in found] == [(4.45, "2026-10-06"), (4.2, "2026-08-01")]
    assert found[0]["magasin"] == "Carrefour Express" and found[0]["ville"] == "Nancy"
    assert "product_code=3017620422003" in seen[0]


async def test_open_prices_errors():
    with pytest.raises(SourceError):
        await FoodSources().open_prices("abc", today="2026-10-15")
    t = transport({"https://prices.openfoodfacts.org": httpx2.Response(503)})
    with pytest.raises(SourceError):
        await FoodSources(transport=t).open_prices("3017620422003", today="2026-10-15")
    t = transport({"https://prices.openfoodfacts.org": httpx2.Response(200, json={"items": "x"})})
    assert await FoodSources(transport=t).open_prices("3017620422003", today="2026-10-15") == []
