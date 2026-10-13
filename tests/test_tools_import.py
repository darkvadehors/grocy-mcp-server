import json

import pytest

from grocy_mcp.tools import ToolFailure
from tests.conftest import ELLI, JOHNNY

pytestmark = pytest.mark.anyio


def posts(fake):
    return [path for method, path in fake.paths() if method == "POST"]


def audit_events(settings):
    return [json.loads(line) for line in settings.audit_path.read_text().splitlines()]


# --- creer_emplacement / creer_categorie (I8) -----------------------------------------------------
async def test_creer_emplacement(service, fake, settings):
    out = await service.creer_emplacement(JOHNNY, "Cave à vin", congelateur=False)
    assert out["statut"] == "ok" and out["cree"] is True
    assert any(loc["name"] == "Cave à vin" and loc["is_freezer"] == 0 for loc in fake.locations)
    event = audit_events(settings)[-1]
    assert (event["outil"], event["statut"]) == ("creer_emplacement", "ok")


async def test_creer_emplacement_congelateur(service, fake):
    await service.creer_emplacement(JOHNNY, "Congélateur", congelateur=True)
    assert any(loc["name"] == "Congélateur" and loc["is_freezer"] == 1 for loc in fake.locations)


async def test_creer_emplacement_existant_renvoye_sans_creation(service, fake):
    out = await service.creer_emplacement(JOHNNY, "  FRIGO ")
    assert out["statut"] == "existe_deja" and out["cree"] is False and out["id"] == 1
    assert posts(fake) == []


async def test_creer_categorie_et_doublon(service, fake):
    first = await service.creer_categorie(JOHNNY, "Boissons")
    assert first["statut"] == "ok" and fake.product_groups[0]["name"] == "Boissons"
    again = await service.creer_categorie(JOHNNY, "boissons", cle_idempotence="autre")
    assert again["statut"] == "existe_deja" and len(fake.product_groups) == 1


@pytest.mark.parametrize("name", ["", "   ", "\x00\x01", "x" * 61])
async def test_noms_invalides_refuses(service, fake, name):
    with pytest.raises(ToolFailure):
        await service.creer_emplacement(JOHNNY, name)
    with pytest.raises(ToolFailure):
        await service.creer_categorie(JOHNNY, name)
    assert posts(fake) == []


async def test_voir_references_inclut_categories(service, fake):
    await service.creer_categorie(JOHNNY, "Boissons")
    refs = await service.voir_references(JOHNNY)
    assert refs["categories"] == ["Boissons"] and "Frigo" in refs["emplacements"]
