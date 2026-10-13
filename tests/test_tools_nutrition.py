import dataclasses
import json
from datetime import UTC, datetime

import httpx2
import pytest

from grocy_mcp.grocy import GrocyClient
from grocy_mcp.sources import FoodSources
from grocy_mcp.tools import ToolFailure, ToolService
from tests.conftest import ELLI, JOHNNY
from tests.fake_grocy import KEY_ELLI_IA, KEY_JOHNNY_IA, TODAY

pytestmark = pytest.mark.anyio
LAIT = {"energy-kcal_100g": 47, "proteins_100g": 3.2, "carbohydrates_100g": 4.8, "sugars_100g": 4.8, "fat_100g": 1.6,
        "saturated-fat_100g": 1, "fiber_100g": 0, "salt_100g": 0.1}


@pytest.fixture
def off():
    """Open Food Facts simulé : code-barres → produit."""
    return {"3017620422003": {"product_name": "Lait demi-écrémé UHT", "product_quantity": 1, "product_quantity_unit": "l", "nutriments": LAIT}}


@pytest.fixture
def nutri(settings, store, box, audit, fake, off, clock):
    clock.t = datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=UTC).timestamp()  # « aujourd'hui » du simulateur

    def handler(request):
        code = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        product = off.get(code)
        return httpx2.Response(200, json={"status": 1, "product": product}) if product else httpx2.Response(404, json={"status": 0})

    settings = dataclasses.replace(settings, household=(("H", 50), ("F", 54), ("F", 20)))
    for uid, name, key in [(3, "johnny-ia", KEY_JOHNNY_IA), (4, "elli-ia", KEY_ELLI_IA)]:
        store.upsert_account(uid, name, box.seal(key, f"grocy-user:{uid}"))
    return ToolService(
        settings, store, box, audit,
        lambda key: GrocyClient(settings.grocy_base_url, key, transport=fake.transport()),
        sources=FoodSources(transport=httpx2.MockTransport(handler)),
    )


def writes(fake):
    return [(m, p) for m, p in fake.paths() if m in ("POST", "PUT")]


def set_data(fake, pid, **values):
    names = {"kcal": "nutri_kcal_100g", "proteines": "nutri_proteines_100g", "poids": "nutri_poids_unite_g",
             "non_alimentaire": "nutri_non_alimentaire", "source": "nutri_source"}
    fake.userfield_values[pid] = {names[k]: str(v) for k, v in values.items()}


async def test_voir_nutrition(nutri, fake):
    assert (await nutri.voir_nutrition(JOHNNY, "lait"))["statut"] == "sans_donnees"
    set_data(fake, 1, kcal=47, proteines=3.2, poids=1000, source="Open Food Facts 3017620422003")
    out = await nutri.voir_nutrition(JOHNNY, "lait")
    assert out["pour_100g"]["kcal"] == 47 and out["poids_unite_g"] == 1000
    assert out["par_unite"]["kcal"] == 470 and out["par_unite"]["proteines"] == 32
    assert "Open Food Facts" in out["source"]


async def test_completer_apercu_puis_confirmation(nutri, fake):
    """N1 + N4 : aperçu sans écriture ; confirmation : champs personnalisés et Énergie native."""
    preview = await nutri.completer_nutrition(JOHNNY, ["lait"])
    line = preview["lignes"][0]
    assert line["statut"] == "trouve" and line["source"] == "Open Food Facts" and line["poids_unite_g"] == 1000
    assert line["approximatif"] is True and writes(fake) == []
    done = await nutri.completer_nutrition(JOHNNY, ["lait"], jeton_confirmation=preview["jeton_confirmation"])
    assert done["statut"] == "ok" and done["lignes"][0]["resultat"] == "ecrit"
    values = fake.userfield_values[1]
    assert values["nutri_kcal_100g"] == "47" and values["nutri_poids_unite_g"] == "1000"
    assert "Open Food Facts 3017620422003" in values["nutri_source"] and "ml≈g" in values["nutri_source"]
    assert fake.products[1]["calories"] == 470
    with pytest.raises(ToolFailure, match="confirmation"):
        await nutri.completer_nutrition(JOHNNY, ["lait"], jeton_confirmation=preview["jeton_confirmation"])


async def test_completer_ne_reecrit_jamais(nutri, fake):
    set_data(fake, 1, kcal=50, source="saisie manuelle")
    preview = await nutri.completer_nutrition(JOHNNY, ["lait"])
    assert preview["lignes"][0]["statut"] == "deja_renseigne" and "jeton_confirmation" not in preview


async def test_completer_sans_code_barres_propose_ciqual(nutri, fake):
    preview = await nutri.completer_nutrition(JOHNNY, ["pâtes"])
    line = preview["lignes"][0]
    assert line["statut"] == "choisir_ciqual" and line["candidats"] and "definir_nutrition" in line["message"]
    assert all({"code", "nom"} <= set(c) for c in line["candidats"])


async def test_completer_valeurs_aberrantes_refusees(nutri, fake, off):
    off["3017620422003"]["nutriments"] = {**LAIT, "energy-kcal_100g": 4000}
    preview = await nutri.completer_nutrition(JOHNNY, ["lait"])
    assert preview["lignes"][0]["statut"] == "refuse" and "900" in preview["lignes"][0]["raison"]


async def test_completer_tous_les_produits_sans_donnees(nutri, fake):
    set_data(fake, 4, kcal=717, poids=250)
    set_data(fake, 3, non_alimentaire=1)
    preview = await nutri.completer_nutrition(JOHNNY)
    names = [line["produit"] for line in preview["lignes"]]
    assert "Beurre doux" not in names and "Yaourt fraise" not in names and "Lait demi-écrémé" in names


async def test_definir_ciqual_valeurs_poids_non_alimentaire(nutri, fake, settings):
    out = await nutri.definir_nutrition(JOHNNY, "pâtes", ciqual=9104, poids_unite_g=500)
    values = fake.userfield_values[5]
    assert out["statut"] == "ok" and values["nutri_kcal_100g"] == "145" and values["nutri_source"].startswith("Ciqual 9104")
    assert fake.products[5]["calories"] == 725
    await nutri.definir_nutrition(JOHNNY, "beurre", valeurs={"kcal": 717, "lipides": 81, "ags": 51})
    assert fake.userfield_values[4]["nutri_source"] == "saisie manuelle"
    await nutri.definir_nutrition(JOHNNY, "yaourt fraise", non_alimentaire=True)
    assert fake.userfield_values[3]["nutri_non_alimentaire"] == "1"
    events = [json.loads(line) for line in settings.audit_path.read_text().splitlines()]
    assert [e["outil"] for e in events if e.get("event") == "action"][-3:] == ["definir_nutrition"] * 3


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"ciqual": 1}, {"valeurs": {"kcal": 4000}}, {"valeurs": {"vitamine": 3}}, {"poids_unite_g": 0}, {"poids_unite_g": 60000}],
)
async def test_definir_refus(nutri, fake, kwargs):
    with pytest.raises(ToolFailure):
        await nutri.definir_nutrition(JOHNNY, "pâtes", **kwargs)
    assert writes(fake) == []


async def test_autonomie_exemple_chiffre(nutri, fake):
    """N6 : lait 2 × 1 000 g à 47 kcal et 3,2 g ; beurre 1 × 250 g à 717 kcal et 0,7 g ; yaourt fraise périmé exclu."""
    set_data(fake, 1, kcal=47, proteines=3.2, poids=1000)
    set_data(fake, 4, kcal=717, proteines=0.7, poids=250)
    set_data(fake, 3, kcal=90, proteines=4, poids=125)  # lot du yaourt fraise périmé (TODAY - 2 jours)
    set_data(fake, 5, non_alimentaire=1)
    fake.add_entry(5, 3, TODAY, 2)  # non alimentaire : ignoré
    out = await nutri.autonomie_alimentaire(JOHNNY)
    assert out["besoins_par_jour"] == {"kcal": 6600, "proteines": 158}
    assert out["total"]["kcal"] == pytest.approx(940 + 1792.5, abs=0.1)  # totaux arrondis à 0,1
    assert out["total"]["proteines"] == pytest.approx(64 + 1.75, abs=0.1)
    assert out["jours"]["energie"] == pytest.approx(2732.5 / 6600, abs=0.05)
    assert out["jours"]["proteines"] == pytest.approx(65.75 / 158, abs=0.05)
    assert out["facteur_limitant"] == "énergie"
    assert [p["nom"] for p in out["sans_donnees"]] == ["Yaourt nature"]
    assert out["couverture_pct"] == 67 and "avertissement" in out
    assert out["perimes_exclus"] == 1
    with_expired = await nutri.autonomie_alimentaire(JOHNNY, inclure_perimes=True)
    assert with_expired["total"]["kcal"] == pytest.approx(2732.5 + 112.5, abs=0.1)


async def test_autonomie_sans_foyer(settings, store, box, audit, fake):
    service = ToolService(settings, store, box, audit, lambda key: GrocyClient(settings.grocy_base_url, key, transport=fake.transport()))
    store.upsert_account(3, "johnny-ia", box.seal(KEY_JOHNNY_IA, "grocy-user:3"))
    out = await service.autonomie_alimentaire(JOHNNY)
    assert out["statut"] == "foyer_non_configure"


async def test_isolation_jeton(nutri, fake):
    preview = await nutri.completer_nutrition(JOHNNY, ["lait"])
    with pytest.raises(ToolFailure, match="confirmation"):
        await nutri.completer_nutrition(ELLI, ["lait"], jeton_confirmation=preview["jeton_confirmation"])
    assert writes(fake) == []
