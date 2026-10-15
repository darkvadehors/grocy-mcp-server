import base64
import json
import time

import httpx2
import pytest

from grocy_mcp import packaging_job
from grocy_mcp.audit import AuditLog
from grocy_mcp.grocy import GrocyClient
from grocy_mcp.packaging_job import JobState, OffCache, run_packaging
from grocy_mcp.sources import FoodSources
from tests.fake_grocy import KEY_AUTO, FakeGrocy

pytestmark = pytest.mark.anyio
RICE, WATER, PEN, FLOUR = "3000000000011", "3000000000028", "3000000000035", "3000000000042"
IMG = "https://images.openfoodfacts.org/images/products/300/000/000/0011/front_fr.3.400.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"1" * 64
RULES = [["en:spring-waters", "01.01"], ["en:rices", "06.01"], ["en:flours", "06.02"]]
NUTRI = {"energy-kcal_100g": 350, "proteins_100g": 7, "carbohydrates_100g": 77, "sugars_100g": 0.5, "fat_100g": 1}


@pytest.fixture
def grocy():
    fake = FakeGrocy()
    fake.quantity_units = [{"id": 1, "name": "Pièce"}, {"id": 2, "name": "Kilogramme"}, {"id": 3, "name": "Gramme"},
                           {"id": 4, "name": "Paquet"}, {"id": 7, "name": "Bouteille"}]
    fake.product_groups = [{"id": 28, "name": "06.01 — Riz, pâtes, semoules et céréales"}, {"id": 6, "name": "01.01 — Eau potable"},
                           {"id": 29, "name": "06.02 — Farines, levures et ingrédients de boulangerie"}]
    fake.products = {
        1: {"id": 1, "name": "Riz noir", "location_id": 1, "qu_id_stock": 1, "qu_id_purchase": 1, "qu_id_price": 1, "active": 1},
        2: {"id": 2, "name": "Eau de source", "location_id": 1, "qu_id_stock": 7, "qu_id_purchase": 7, "qu_id_price": 7, "active": 1},
        3: {"id": 3, "name": "Marqueur", "location_id": 1, "qu_id_stock": 1, "qu_id_purchase": 1, "qu_id_price": 1, "active": 1},
        4: {"id": 4, "name": "Farine", "location_id": 1, "qu_id_stock": 2, "qu_id_purchase": 2, "qu_id_price": 2, "active": 1,
            "picture_file_name": "farine.jpg", "calories": 3640},
    }
    fake.barcodes = {RICE: 1, WATER: 2, PEN: 3, FLOUR: 4}
    fake.userfield_values = {3: {"nutri_non_alimentaire": "1"}}
    fake.entries = []
    return fake


@pytest.fixture
def off_db():
    return {
        RICE: {"quantity": "500 g", "nutriments": NUTRI, "image_front_url": IMG, "categories_tags": ["en:cereals", "en:rices"]},
        WATER: {"image_front_url": IMG.replace("0011", "0028"), "categories_tags": ["en:waters", "en:spring-waters"],
                "nutriments": {"energy-kcal_100g": 0}},
        PEN: {"image_front_url": IMG.replace("0011", "0035"), "nutriments": NUTRI},
        FLOUR: {"nutriments": NUTRI, "categories_tags": ["en:flours"], "image_front_url": IMG.replace("0011", "0042")},
    }


@pytest.fixture
def calls():
    return []


@pytest.fixture
def images():
    return {}


@pytest.fixture
def sources(off_db, calls, images):
    def handler(request):
        url = str(request.url)
        if request.url.host == "images.openfoodfacts.org":
            calls.append(("image", request.url.path))
            return images.get(url, httpx2.Response(200, content=JPEG))
        code = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        calls.append(("off", code))
        product = off_db.get(code)
        return httpx2.Response(200, json={"status": 1, "product": product}) if product else httpx2.Response(404, json={"status": 0})

    return FoodSources(transport=httpx2.MockTransport(handler))


def client(fake):
    return GrocyClient("https://grocy.example.test", KEY_AUTO, transport=fake.transport())


async def run(fake, sources, tmp_path, **kwargs):
    kwargs.setdefault("state", JobState(tmp_path / "traites.json"))
    return await run_packaging(client(fake), sources, kwargs.pop("cache", OffCache(tmp_path / "off.json")),
                               enrich=kwargs.pop("enrich", True), rules=kwargs.pop("rules", RULES), **kwargs)


async def test_fills_only_empty_fields(grocy, sources, calls, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    report = await run(grocy, sources, tmp_path, audit=audit)
    rice, water, pen, flour = (grocy.products[i] for i in (1, 2, 3, 4))
    # riz : contenance (nom vide, Open Food Facts « 500 g »), puis catégorie, nutrition au poids du sachet, photo
    assert rice["product_group_id"] == 28 and grocy.userfield_values[1]["nutri_kcal_100g"] == "350"
    assert grocy.userfield_values[1]["nutri_poids_unite_g"] == "500" and rice["calories"] == 1750
    assert "Open Food Facts 3000000000011" in grocy.userfield_values[1]["nutri_source"]
    assert rice["picture_file_name"].startswith("off-1-3000000000011-") and grocy.files[rice["picture_file_name"]] == JPEG
    # eau : catégorie la plus précise ; stockée à la bouteille sans contenance : aucune énergie inventée
    assert water["product_group_id"] == 6 and "calories" not in water and 2 not in grocy.userfield_values
    # marqueur non alimentaire : photo seulement, aucune règle de catégorie
    assert pen["picture_file_name"] and grocy.userfield_values[3] == {"nutri_non_alimentaire": "1"} and "product_group_id" not in pen
    # farine : photo et énergie saisies à la main restent, la catégorie vide est remplie
    assert flour["picture_file_name"] == "farine.jpg" and flour["calories"] == 3640 and flour["product_group_id"] == 29
    assert len(grocy.files) == 3
    assert sorted(c for k, c in calls if k == "off") == sorted([RICE, WATER, PEN, FLOUR])  # E3 : une requête par code
    lines = {line["produit"]: line for line in report["produits"]}
    assert lines["Riz noir"]["enrichi"] == ["categorie", "nutrition", "photo"]
    events = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    enriched = [e for e in events if e["event"] == "enrichissement"]
    assert {e["produit_id"] for e in enriched} == {1, 2, 3, 4} and all(e["compte_id"] == 9 for e in enriched)


async def test_without_option_nothing_is_enriched(grocy, sources, calls, tmp_path):
    await run(grocy, sources, tmp_path, enrich=False)
    assert grocy.files == {} and all("product_group_id" not in p for p in grocy.products.values())
    assert not any(k == "image" for k, _ in calls)


async def test_dry_run_writes_nothing(grocy, sources, calls, tmp_path):
    report = await run(grocy, sources, tmp_path, dry_run=True)
    assert grocy.files == {} and grocy.userfield_values == {3: {"nutri_non_alimentaire": "1"}}
    assert all("product_group_id" not in p for p in list(grocy.products.values())[:3])
    assert not any(k == "image" for k, _ in calls)
    lines = {line["produit"]: line for line in report["produits"]}
    assert lines["Eau de source"]["enrichi"] == ["categorie", "photo"]


async def test_never_refills_what_a_person_emptied(grocy, sources, calls, tmp_path):
    """Review Focus 3."""
    await run(grocy, sources, tmp_path)
    grocy.products[1]["picture_file_name"] = None
    grocy.products[1]["product_group_id"] = None
    grocy.userfield_values[1] = {}
    grocy.products[1]["calories"] = 0
    calls.clear()
    report = await run(grocy, sources, tmp_path)
    assert grocy.products[1]["picture_file_name"] is None and grocy.products[1]["product_group_id"] is None
    assert grocy.userfield_values[1] == {} and len(grocy.files) == 3 and calls == []
    assert "enrichi" not in {line["produit"]: line for line in report["produits"]}["Riz noir"]


async def test_old_cache_is_read_again_once(grocy, sources, calls, tmp_path):
    """Review Focus 4 : une entrée de cache d'avant l'enrichissement (contenance seule) est relue une seule fois."""
    path = tmp_path / "off.json"
    path.write_text(json.dumps({RICE: {"t": time.time(), "q": "500 g"}}))
    await run(grocy, sources, tmp_path, cache=OffCache(path), state=None)
    assert [c for k, c in calls if k == "off"].count(RICE) == 1 and grocy.products[1]["product_group_id"] == 28
    calls.clear()
    await run(grocy, sources, tmp_path, cache=OffCache(path), state=None)
    assert not any(k == "off" for k, _ in calls)


async def test_broken_image_is_dropped_not_retried(grocy, sources, calls, images, tmp_path):
    """Review Focus 1 : une « photo » qui n'en est pas une n'est jamais déposée, ni retéléchargée à chaque passage."""
    images[IMG] = httpx2.Response(200, content=b"<svg onload=alert(1)>")
    await run(grocy, sources, tmp_path)
    assert grocy.products[1].get("picture_file_name") is None and grocy.products[1]["product_group_id"] == 28
    calls.clear()
    await run(grocy, sources, tmp_path)
    assert not any(k == "image" for k, _ in calls)


async def test_error_on_one_product_keeps_the_others(grocy, sources, tmp_path):
    grocy.userfield_names.discard("nutri_source")  # Grocy refuse l'écriture des apports (400)
    audit = AuditLog(tmp_path / "audit.jsonl")
    state = JobState(tmp_path / "traites.json")
    report = await run(grocy, sources, tmp_path, audit=audit, state=state)
    lines = {line["produit"]: line for line in report["produits"]}
    assert lines["Riz noir"]["enrichissement"] == "erreur" and grocy.products[1]["product_group_id"] == 28
    assert grocy.products[2]["product_group_id"] == 6 and grocy.products[2]["picture_file_name"]
    events = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    partial = [e for e in events if e["event"] == "enrichissement_partiel"]
    assert partial and partial[0]["produit_id"] == 1 and partial[0]["champs"] == ["categorie"]
    assert JobState(tmp_path / "traites.json").filled[1] == {"categorie"}


async def test_lookups_share_the_budget(grocy, sources, calls, tmp_path, monkeypatch):
    monkeypatch.setattr(packaging_job, "MAX_LOOKUPS_PER_PASS", 2)
    report = await run(grocy, sources, tmp_path)
    assert len([c for k, c in calls if k == "off"]) == 2
    assert sum(line["statut"] == "reporte" for line in report["produits"]) == 2


async def test_upload_picture_name_and_content(grocy):
    name = await client(grocy).upload_product_picture("off-1-3000000000011-1.jpg", JPEG)
    assert grocy.files[name] == JPEG
    with pytest.raises(ValueError):
        await client(grocy).upload_product_picture("../etc/passwd", JPEG)
    with pytest.raises(ValueError):
        await client(grocy).set_product_fields(1, {"name": "x"})
    encoded = base64.b64encode(b"off-1-3000000000011-1.jpg").decode()
    assert ("PUT", f"/api/files/productpictures/{encoded}") in grocy.calls


async def test_reused_product_id_starts_fresh(grocy, sources, tmp_path):
    """L'app Grocy Mobile réutilise le numéro d'un produit supprimé : la mémoire de l'ancien ne vaut pas pour le nouveau."""
    grocy.products[1]["row_created_timestamp"] = "2026-10-13 10:00:00"
    await run(grocy, sources, tmp_path)
    assert JobState(tmp_path / "traites.json").filled[1] == {"categorie", "nutrition", "photo"}
    grocy.products[1] = {"id": 1, "name": "Riz noir", "location_id": 1, "qu_id_stock": 1, "qu_id_purchase": 1,
                         "qu_id_price": 1, "active": 1, "row_created_timestamp": "2026-10-15 08:17:12"}
    grocy.conversions = [c for c in grocy.conversions if c.get("product_id") != 1]
    grocy.userfield_values.pop(1, None)
    await run(grocy, sources, tmp_path)
    assert grocy.products[1]["product_group_id"] == 28 and grocy.products[1]["picture_file_name"]
    assert [c["factor"] for c in grocy.conversions if c.get("product_id") == 1][:1] == [500]  # conditionné à nouveau


async def test_same_product_keeps_its_memory(grocy, sources, tmp_path):
    grocy.products[1]["row_created_timestamp"] = "2026-10-13 10:00:00"
    await run(grocy, sources, tmp_path)
    grocy.products[1]["product_group_id"] = None
    await run(grocy, sources, tmp_path)
    assert grocy.products[1]["product_group_id"] is None


async def test_pause_between_open_food_facts_requests(grocy, sources, tmp_path, monkeypatch):
    """Open Food Facts limite le débit (429) : une pause entre deux requêtes du passage, jamais avant la première."""
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(packaging_job, "LOOKUP_PAUSE", 1.0)
    monkeypatch.setattr(packaging_job.asyncio, "sleep", fake_sleep)
    await run(grocy, sources, tmp_path)
    assert pauses == [1.0, 1.0, 1.0]  # 4 codes, 4 requêtes
