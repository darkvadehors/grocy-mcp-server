import json

import httpx2
import pytest

from grocy_mcp.grocy import GrocyClient
from grocy_mcp.packaging import resolve_unit
from grocy_mcp.sources import FoodSources
from grocy_mcp.tools import ToolFailure, ToolService
from tests.conftest import JOHNNY
from tests.fake_grocy import KEY_ELLI_IA, KEY_JOHNNY_IA

pytestmark = pytest.mark.anyio


@pytest.fixture
def pack(settings, store, box, audit, fake):
    fake.quantity_units = [
        {"id": 1, "name": "Pièce", "name_plural": "Pièces"}, {"id": 2, "name": "Gramme", "name_plural": "Grammes"},
        {"id": 3, "name": "Pack", "name_plural": "Packs"}, {"id": 4, "name": "Bouteille", "name_plural": "Bouteilles"},
        {"id": 5, "name": "Centilitre", "name_plural": "Centilitres"}, {"id": 6, "name": "Paquet", "name_plural": "Paquets"},
    ]
    fake.products[1]["name"] = "Lait demi-écrémé 1 L"
    fake.products[5]["name"] = "Pâtes 500 g"
    fake.products[6] = {"id": 6, "name": "Tourtel 6X27,5CL", "location_id": 1, "qu_id_stock": 4, "qu_id_purchase": 3, "active": 1}
    fake.products[7] = {"id": 7, "name": "Morceaux de noix", "location_id": 2, "qu_id_stock": 2, "qu_id_purchase": 2, "active": 1}
    for uid, name, key in [(3, "johnny-ia", KEY_JOHNNY_IA), (4, "elli-ia", KEY_ELLI_IA)]:
        store.upsert_account(uid, name, box.seal(key, f"grocy-user:{uid}"))
    return ToolService(
        settings, store, box, audit,
        lambda key: GrocyClient(settings.grocy_base_url, key, transport=fake.transport()),
        sources=FoodSources(transport=httpx2.MockTransport(lambda r: httpx2.Response(404, json={"status": 0}))),
    )


def test_resolve_unit():
    units = {1: "Pièce", 2: "Gramme", 5: "Centilitre", 7: "Bouteille"}
    plurals = {7: "Bouteilles"}
    assert resolve_unit(units, "g") == 2 and resolve_unit(units, "grammes") == 2 and resolve_unit(units, "cL") == 5
    assert resolve_unit(units, "piece") == 1 and resolve_unit(units, "bouteilles", plurals) == 7
    assert resolve_unit(units, "kg") is None and resolve_unit(units, "") is None


async def test_voir_conditionnements_manquants(pack, fake):
    out = await pack.voir_conditionnements_manquants(JOHNNY)
    by_name = {line["produit"]: line for line in out["produits"]}
    assert by_name["Pâtes 500 g"]["statut"] == "a_ecrire" and "automatiquement" in by_name["Pâtes 500 g"]["explication"]
    assert by_name["Tourtel 6X27,5CL"]["statut"] == "lot" and "definir_conditionnement" in by_name["Tourtel 6X27,5CL"]["explication"]
    assert by_name["Morceaux de noix"]["statut"] == "inconnu"
    assert [m for m, _ in fake.paths() if m in ("POST", "PUT")] == []  # lecture seule


async def test_definir_conditionnement_paquet(pack, fake, settings):
    out = await pack.definir_conditionnement(JOHNNY, "noix", 250, "g", par="Paquet")
    assert out["statut"] == "ok" and "1 Paquet = 250 Gramme" in out["message"]
    assert [(c["product_id"], c["from_qu_id"], c["to_qu_id"], c["factor"]) for c in fake.conversions] == [(7, 6, 2, 250)]
    events = [json.loads(line) for line in settings.audit_path.read_text().splitlines()]
    written = [e for e in events if e["event"] == "conditionnement"]
    assert len(written) == 1 and written[0]["compte_id"] == 3 and written[0]["produit_id"] == 7


async def test_definir_conditionnement_lot(pack, fake):
    await pack.definir_conditionnement(JOHNNY, "tourtel", 6, "bouteilles", par="pack")
    await pack.definir_conditionnement(JOHNNY, "tourtel", 27.5, "cl")  # par défaut : l'unité de stock (Bouteille)
    assert sorted((c["from_qu_id"], c["to_qu_id"], c["factor"]) for c in fake.conversions) == [(3, 4, 6), (4, 5, 27.5)]


async def test_definir_conditionnement_existant(pack, fake):
    await pack.definir_conditionnement(JOHNNY, "noix", 250, "g", par="Paquet")
    # nouvelle clé d'idempotence : sinon la même demande dans les 10 minutes renvoie simplement le premier résultat
    same = await pack.definir_conditionnement(JOHNNY, "noix", 250, "g", par="Paquet", cle_idempotence="k2")
    assert same["statut"] == "existe_deja" and len(fake.conversions) == 1
    with pytest.raises(ToolFailure, match="existe déjà"):
        await pack.definir_conditionnement(JOHNNY, "noix", 200, "g", par="Paquet", cle_idempotence="k3")


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("noix", 0, "g"), "quantite"),
        (("noix", -3, "g"), "quantite"),
        (("noix", 2_000_000, "g"), "quantite"),
        (("noix", 250, "g", "Gramme"), "différentes"),
        (("noix", 250, "boisseau"), "Unité"),
        (("noix", 250, "g", "caisse"), "Unité"),
    ],
)
async def test_definir_conditionnement_refus(pack, fake, args, error):
    with pytest.raises(ToolFailure, match=error):
        await pack.definir_conditionnement(JOHNNY, *args)
    assert fake.conversions == []


async def test_definir_conditionnement_produit_ambigu(pack, fake):
    out = await pack.definir_conditionnement(JOHNNY, "yaourt", 125, "g")
    assert out["statut"] == "ambigu" and fake.conversions == []


async def test_definir_conditionnement_par_defaut_unite_de_stock(pack, fake):
    fake.products[8] = {"id": 8, "name": "Riz noir", "location_id": 2, "qu_id_stock": 1, "qu_id_purchase": 1, "active": 1}
    await pack.definir_conditionnement(JOHNNY, "riz noir", 500, "g")
    assert [(c["from_qu_id"], c["to_qu_id"], c["factor"]) for c in fake.conversions] == [(1, 2, 500)]


async def test_definir_conditionnement_refuse_deux_unites_metriques(pack, fake):
    """I3 de la revue : « 1 Gramme = 250 Gramme » ou « 1 kg = 250 g » propre au produit fausserait le stock."""
    fake.quantity_units.append({"id": 7, "name": "Kilogramme", "name_plural": "Kilogrammes"})
    fake.products[7]["qu_id_stock"] = 7
    with pytest.raises(ToolFailure, match="poids ou volume"):
        await pack.definir_conditionnement(JOHNNY, "noix", 250, "g")
    assert fake.conversions == []


async def test_definir_conditionnement_refuse_une_contradiction(pack, fake):
    """I4 : sens inverse ou même dimension déjà réglée pour cette unité."""
    fake.products[8] = {"id": 8, "name": "Riz noir", "location_id": 2, "qu_id_stock": 1, "qu_id_purchase": 1, "active": 1}
    fake.conversions.append({"id": 1, "product_id": 8, "from_qu_id": 1, "to_qu_id": 2, "factor": 500})
    fake.quantity_units.append({"id": 7, "name": "Kilogramme", "name_plural": "Kilogrammes"})
    with pytest.raises(ToolFailure, match="existe déjà"):
        await pack.definir_conditionnement(JOHNNY, "riz noir", 1, "kg")
    with pytest.raises(ToolFailure, match="existe déjà"):
        await pack.definir_conditionnement(JOHNNY, "riz noir", 0.002, "Pièce", par="g")
    assert len(fake.conversions) == 1
