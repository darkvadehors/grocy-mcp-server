import json

import httpx2
import pytest

from grocy_mcp.audit import AuditLog
from grocy_mcp.grocy import GrocyClient
from grocy_mcp.packaging_job import JobState, OffCache, run_packaging
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
    """Conversions propres au produit, sans les inverses que Grocy ajoute de lui-même."""
    rows = [c for c in fake.conversions if c.get("product_id") == pid]
    firsts = []
    for c in sorted(rows, key=lambda c: c["id"]):
        if not any(f[0] == c["to_qu_id"] and f[1] == c["from_qu_id"] for f in firsts):
            firsts.append((c["from_qu_id"], c["to_qu_id"], c["factor"]))
    return sorted(firsts)


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


async def test_nutrition_weight_never_used_for_a_weighed_product(grocy, sources):
    """C1 de la revue : produit stocké en g ou kg, poids nutrition = 1 g ou 1000 g par unité : jamais « 1 Paquet = 1 g »."""
    grocy.userfield_values[4] = {"nutri_poids_unite_g": "1"}
    grocy.userfield_values[2] = {"nutri_poids_unite_g": "1000"}
    grocy.barcodes = {}
    report = await run_packaging(client(grocy), sources, OffCache(None))
    assert own(grocy, 4) == [] and own(grocy, 2) == []
    statuses = {line["produit"]: line["statut"] for line in report["produits"]}
    assert statuses["Morceaux de noix"] == "inconnu" and statuses["Graines de courge"] == "inconnu"


async def test_partial_write_is_audited_and_completed(grocy, sources, tmp_path):
    """I1 : échec sur la deuxième conversion : trace dans l'audit, puis complétée au passage suivant."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    grocy.fail_second_conversion_for = 2
    cache = OffCache(tmp_path / "off.json")  # la contenance Open Food Facts reste connue grâce au cache du passage
    report = await run_packaging(client(grocy), sources, cache, audit=audit, state=JobState(tmp_path / "traites.json"))
    statuses = {line["produit"]: line["statut"] for line in report["produits"]}
    assert statuses["Graines de courge"] == "erreur" and len(own(grocy, 2)) == 1
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(e["event"] == "conditionnement_partiel" and e["produit_id"] == 2 for e in events)
    grocy.fail_second_conversion_for = None
    await run_packaging(client(grocy), sources, OffCache(tmp_path / "off.json"), audit=audit, state=JobState(tmp_path / "traites.json"))
    assert own(grocy, 2) == [(1, 2, 0.25), (4, 2, 0.25)] and grocy.products[2]["qu_id_purchase"] == 4


@pytest.mark.parametrize(
    "body",
    [
        "[1, 2, 3]",
        '{"status": 1, "product": {"quantity": 250}}',
        '{"status": 1, "product": ' + "[" * 200_000 + "]" * 200_000 + "}",
        "pas du json",
    ],
)
async def test_hostile_off_answer_never_stops_the_pass(grocy, tmp_path, body):
    """I2 : réponse Open Food Facts piégée : le passage continue, les autres produits sont traités."""
    hostile = FoodSources(transport=httpx2.MockTransport(lambda r: httpx2.Response(200, content=body.encode())))
    report = await run_packaging(client(grocy), hostile, OffCache(None))
    assert own(grocy, 1) == [(1, 3, 500)]
    assert {line["produit"] for line in report["produits"]} >= {"Graines de courge", "Morceaux de noix"}


async def test_off_lookups_are_capped_per_pass(grocy, sources, off_calls, monkeypatch):
    """M1 : au-delà du plafond, le produit est reporté au passage suivant, jamais décidé sans Open Food Facts."""
    import grocy_mcp.packaging_job as job

    monkeypatch.setattr(job, "MAX_LOOKUPS_PER_PASS", 1)
    report = await run_packaging(client(grocy), sources, OffCache(None))
    statuses = {line["produit"]: line["statut"] for line in report["produits"]}
    assert len(off_calls) == 1 and statuses["Morceaux de noix"] == "reporte"


async def test_removed_conversion_is_never_recreated(grocy, sources, tmp_path):
    """Une contenance écrite par le passage puis supprimée par la personne n'est jamais recréée."""
    state = JobState(tmp_path / "traites.json")
    await run_packaging(client(grocy), sources, OffCache(None), state=state)
    assert own(grocy, 1) == [(1, 3, 500)]
    grocy.conversions = [c for c in grocy.conversions if c.get("product_id") != 1]  # supprimée dans Grocy
    report = await run_packaging(client(grocy), sources, OffCache(None), state=JobState(tmp_path / "traites.json"))
    assert own(grocy, 1) == []
    assert {line["produit"]: line["statut"] for line in report["produits"]}["Riz noir 500 g"] == "retire"


async def test_ignored_products_are_left_alone(grocy, sources, tmp_path):
    state = JobState(tmp_path / "traites.json")
    state.ignore([1])
    report = await run_packaging(client(grocy), sources, OffCache(None), state=state)
    assert own(grocy, 1) == [] and {line["produit"]: line["statut"] for line in report["produits"]}["Riz noir 500 g"] == "retire"
    assert JobState(tmp_path / "traites.json").done == {1, 2}  # mémorisé (fichier) : 1 ignoré, 2 écrit


async def test_dry_run_does_not_remember(grocy, sources, tmp_path):
    state = JobState(tmp_path / "traites.json")
    await run_packaging(client(grocy), sources, OffCache(None), state=state, dry_run=True)
    assert JobState(tmp_path / "traites.json").done == set()
