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
