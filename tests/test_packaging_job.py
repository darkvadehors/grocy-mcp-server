import json

import httpx2
import pytest

from grocy_mcp.audit import AuditLog
from grocy_mcp.grocy import GrocyClient
from grocy_mcp.packaging_job import OffCache, run_packaging
from grocy_mcp.sources import FoodSources
from tests.fake_grocy import KEY_AUTO, FakeGrocy

pytestmark = pytest.mark.anyio
UNITS = [
    {"id": 1, "name": "Pièce"}, {"id": 2, "name": "Kilogramme"}, {"id": 3, "name": "Gramme"}, {"id": 4, "name": "Paquet"},
    {"id": 5, "name": "Centilitre"}, {"id": 6, "name": "Pack"}, {"id": 7, "name": "Bouteille"},
]


@pytest.fixture
def grocy():
    fake = FakeGrocy()
    fake.quantity_units = [dict(u) for u in UNITS]
    fake.products = {
        1: {"id": 1, "name": "Riz noir 500 g", "location_id": 1, "qu_id_stock": 1, "qu_id_purchase": 1, "qu_id_price": 1, "active": 1},
        2: {"id": 2, "name": "Graines de courge", "location_id": 1, "qu_id_stock": 2, "qu_id_purchase": 2, "qu_id_price": 2, "active": 1},
        3: {"id": 3, "name": "Tourtel 6X27,5CL 1650 ml", "location_id": 1, "qu_id_stock": 1, "qu_id_purchase": 1, "qu_id_price": 1, "active": 1},
        4: {"id": 4, "name": "Morceaux de noix", "location_id": 1, "qu_id_stock": 3, "qu_id_purchase": 3, "qu_id_price": 3, "active": 1},
        5: {"id": 5, "name": "Bière 75 cl", "location_id": 1, "qu_id_stock": 6, "qu_id_purchase": 6, "qu_id_price": 6, "active": 1},
    }
    fake.barcodes = {"3000000000002": 2, "3000000000004": 4}
    fake.conversions = [{"id": 1, "product_id": 5, "from_qu_id": 6, "to_qu_id": 7, "factor": 6}]  # réglée à la main
    fake.entries = []
    return fake


@pytest.fixture
def off_db():
    return {"3000000000002": {"quantity": "250 g"}}


@pytest.fixture
def off_calls():
    return []


@pytest.fixture
def sources(off_db, off_calls):
    def handler(request):
        code = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        off_calls.append(code)
        if code == "3000000000004":
            return httpx2.Response(500)  # Review Focus 4 : panne d'Open Food Facts
        product = off_db.get(code)
        return httpx2.Response(200, json={"status": 1, "product": product}) if product else httpx2.Response(404, json={"status": 0})

    return FoodSources(transport=httpx2.MockTransport(handler))


def client(fake):
    return GrocyClient("https://grocy.example.test", KEY_AUTO, transport=fake.transport())


def own(fake, pid):
    return sorted((c["from_qu_id"], c["to_qu_id"], c["factor"]) for c in fake.conversions if c.get("product_id") == pid)


async def test_run_writes_only_certain_contents(grocy, sources, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    report = await run_packaging(client(grocy), sources, OffCache(tmp_path / "off.json"), audit=audit)
    assert own(grocy, 1) == [(1, 3, 500)]  # riz : 1 pièce = 500 g (nom)
    assert own(grocy, 2) == [(1, 2, 0.25), (4, 2, 0.25)]  # graines : contenance Open Food Facts, stock en kg
    assert grocy.products[2]["qu_id_purchase"] == 4 and grocy.products[2]["qu_id_price"] == 4
    assert own(grocy, 3) == [] and own(grocy, 4) == []  # lot ; noix sans contenance (Open Food Facts en panne)
    assert own(grocy, 5) == [(6, 7, 6)]  # conversion manuelle intacte
    statuses = {line["produit"]: line["statut"] for line in report["produits"]}
    assert statuses["Riz noir 500 g"] == "ecrit" and statuses["Graines de courge"] == "ecrit"
    assert statuses["Tourtel 6X27,5CL 1650 ml"] == "lot" and statuses["Morceaux de noix"] == "inconnu"
    assert statuses["Bière 75 cl"] == "deja"
    assert report["ecrits"] == 2
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["conditionnement", "conditionnement"]
    assert {e["produit_id"] for e in events} == {1, 2} and all(e["compte_id"] == 9 for e in events)


async def test_run_is_idempotent(grocy, sources, tmp_path, off_calls):
    """Review Focus 3 : relance sans doublon ; Open Food Facts n'est pas réinterrogé (C7)."""
    cache = OffCache(tmp_path / "off.json")
    await run_packaging(client(grocy), sources, cache)
    first = [c for c in grocy.conversions]
    report = await run_packaging(client(grocy), sources, OffCache(tmp_path / "off.json"))
    assert grocy.conversions == first and report["ecrits"] == 0
    assert off_calls.count("3000000000002") == 1


async def test_failed_off_lookup_is_retried_later(grocy, sources, tmp_path, off_calls):
    cache = OffCache(tmp_path / "off.json")
    await run_packaging(client(grocy), sources, cache)
    await run_packaging(client(grocy), sources, OffCache(tmp_path / "off.json"))
    assert off_calls.count("3000000000004") == 2  # une panne n'est pas mise en cache


async def test_off_cache_expires(tmp_path):
    now = [1_000_000.0]
    cache = OffCache(tmp_path / "off.json", clock=lambda: now[0])
    cache.put("123", "250 g")
    assert cache.get("123") == (True, "250 g")
    now[0] += 8 * 86400
    assert OffCache(tmp_path / "off.json", clock=lambda: now[0]).get("123") == (False, None)


async def test_off_cache_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "off.json"
    path.write_text("{pas du json")
    assert OffCache(path).get("123") == (False, None)


async def test_dry_run_writes_nothing(grocy, sources, tmp_path):
    report = await run_packaging(client(grocy), sources, OffCache(None), dry_run=True)
    assert [m for m, _ in grocy.paths() if m in ("POST", "PUT")] == []
    assert {line["statut"] for line in report["produits"]} >= {"a_ecrire", "lot", "deja"}


async def test_nutrition_weight_is_a_fallback(grocy, sources, tmp_path):
    grocy.userfield_values[4] = {"nutri_poids_unite_g": "250"}
    grocy.products[4]["qu_id_stock"] = 1  # noix comptées à la pièce
    await run_packaging(client(grocy), sources, OffCache(None))
    assert own(grocy, 4) == [(1, 3, 250)]


async def test_one_failing_product_does_not_stop_the_others(grocy, sources, tmp_path):
    grocy.fail_conversion_for = 1
    report = await run_packaging(client(grocy), sources, OffCache(None))
    statuses = {line["produit"]: line["statut"] for line in report["produits"]}
    assert statuses["Riz noir 500 g"] == "erreur" and statuses["Graines de courge"] == "ecrit"


async def test_inactive_products_are_ignored(grocy, sources):
    grocy.products[1]["active"] = 0
    report = await run_packaging(client(grocy), sources, OffCache(None))
    assert "Riz noir 500 g" not in {line["produit"] for line in report["produits"]}
