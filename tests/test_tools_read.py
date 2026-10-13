import pytest

from grocy_mcp.tools import Caller, ToolFailure, clean_text, fold
from tests.conftest import JOHNNY

pytestmark = pytest.mark.anyio
PRODUCTS = ("GET", "/api/objects/products")


def test_fold_and_clean():
    assert fold("  Pâtes   Complètes ") == "pates completes"
    assert clean_text("A\x1b[31mB‮C\x00") == "A[31mBC"
    assert len(clean_text("x" * 500)) == 120
    assert clean_text(None) == ""


async def test_rechercher_produit_par_nom(service, fake):
    out = await service.rechercher_produit(JOHNNY, "yaourt")
    assert [p["nom"] for p in out["produits"]] == ["Yaourt nature", "Yaourt fraise"]
    assert out["tronque"] is False
    assert fake.paths() == [PRODUCTS]
    assert [p["nom"] for p in (await service.rechercher_produit(JOHNNY, "PATES"))["produits"]] == ["Pâtes"]


async def test_rechercher_produit_par_code_barres(service, fake):
    out = await service.rechercher_produit(JOHNNY, "3017620422003")
    assert out["produits"] == [{"id": 1, "nom": "Lait demi-écrémé"}]
    assert fake.paths() == [("GET", "/api/stock/products/by-barcode/3017620422003")]
    assert (await service.rechercher_produit(JOHNNY, "12345678"))["produits"] == []
    with pytest.raises(ToolFailure):
        await service.rechercher_produit(JOHNNY, "   ")


async def test_voir_stock(service, fake):
    out = await service.voir_stock(JOHNNY)
    assert [p["nom"] for p in out["produits"]] == ["Beurre doux", "Lait demi-écrémé", "Yaourt fraise", "Yaourt nature"]
    assert out["produits"][1]["quantite"] == 2
    assert fake.paths() == [("GET", "/api/stock")]
    fake.calls.clear()
    assert len((await service.voir_stock(JOHNNY, "frigo"))["produits"]) == 4
    assert fake.paths() == [("GET", "/api/stock"), ("GET", "/api/objects/locations")]
    assert (await service.voir_stock(JOHNNY, "Placard"))["produits"] == []
    with pytest.raises(ToolFailure, match="Emplacements : Frigo, Placard"):
        await service.voir_stock(JOHNNY, "Cave")


async def test_voir_peremptions(service, fake):
    out = await service.voir_peremptions(JOHNNY)
    assert [p["nom"] for p in out["bientot"]] == ["Lait demi-écrémé"]
    assert [p["nom"] for p in out["depasses"]] == ["Yaourt fraise"]
    assert [p["nom"] for p in out["sous_le_minimum"]] == ["Beurre doux"]
    assert fake.paths() == [("GET", "/api/stock/volatile")]
    assert [p["nom"] for p in (await service.voir_peremptions(JOHNNY, 10))["bientot"]] == ["Lait demi-écrémé", "Yaourt nature"]
    for bad in (0, 61, -3):
        with pytest.raises(ToolFailure):
            await service.voir_peremptions(JOHNNY, bad)


async def test_voir_produit(service, fake):
    out = await service.voir_produit(JOHNNY, "lait")
    assert out["statut"] == "ok" and out["quantite"] == 2 and out["emplacement_par_defaut"] == "Frigo"
    assert fake.paths() == [
        PRODUCTS,
        ("GET", "/api/stock/products/1"),
        ("GET", "/api/stock/products/1/entries"),
        ("GET", "/api/stock/products/1/price-history"),
    ]


async def test_voir_produit_ambigu_ou_introuvable(service, fake):
    out = await service.voir_produit(JOHNNY, "yaourt")
    assert out["statut"] == "ambigu"
    assert [c["nom"] for c in out["candidats"]] == ["Yaourt nature", "Yaourt fraise"]
    assert fake.paths() == [PRODUCTS]
    assert (await service.voir_produit(JOHNNY, "chocolat"))["statut"] == "introuvable"
    assert (await service.voir_produit(JOHNNY, "#2"))["produit"]["nom"] == "Yaourt nature"
    assert (await service.voir_produit(JOHNNY, "yaourt nature"))["statut"] == "ok"


async def test_voir_liste_courses(service, fake):
    fake.shopping.append({"id": 7, "product_id": 1, "amount": 2.0, "note": "bio", "shopping_list_id": 1})
    fake.shopping.append({"id": 8, "product_id": None, "amount": 1.0, "note": "piles", "shopping_list_id": 1})
    out = await service.voir_liste_courses(JOHNNY)
    assert out["articles"] == [
        {"id": 7, "produit_id": 1, "produit": "Lait demi-écrémé", "quantite": 2, "note": "bio"},
        {"id": 8, "produit_id": None, "produit": None, "quantite": 1, "note": "piles"},
    ]
    assert fake.paths() == [("GET", "/api/objects/shopping_list"), PRODUCTS]


async def test_recettes_faisables(service, fake):
    out = await service.recettes_faisables(JOHNNY)
    assert out["recettes"] == [{"recette_id": 1, "nom": "Crêpes", "faisable": False, "produits_manquants": 1}]
    assert fake.paths() == [("GET", "/api/recipes/fulfillment"), ("GET", "/api/objects/recipes")]


async def test_voir_references(service, fake):
    out = await service.voir_references(JOHNNY)
    assert out == {"emplacements": ["Frigo", "Placard"], "unites": ["Pièce"]}
    assert fake.paths() == [("GET", "/api/objects/locations"), ("GET", "/api/objects/quantity_units")]


async def test_free_text_is_cleaned(service, fake):
    fake.products[2]["name"] = "Yaourt\x1b nature‮" + "x" * 300
    out = await service.rechercher_produit(JOHNNY, "nature")
    assert "\x1b" not in out["produits"][0]["nom"] and len(out["produits"][0]["nom"]) == 120


async def test_grocy_unavailable(service, fake):
    fake.next_failure = 503
    with pytest.raises(ToolFailure, match="ne répond pas"):
        await service.voir_stock(JOHNNY)
    fake.next_failure = "timeout"
    with pytest.raises(ToolFailure, match="ne répond pas"):
        await service.voir_stock(JOHNNY)


async def test_unknown_account(service):
    with pytest.raises(ToolFailure, match="Reconnectez"):
        await service.voir_stock(Caller(99, "f", "c1"))
