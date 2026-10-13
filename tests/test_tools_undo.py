import json

import pytest

from grocy_mcp.grocy import GrocyClient
from grocy_mcp.tools import ToolFailure
from tests.conftest import ELLI, JOHNNY
from tests.fake_grocy import KEY_JOHNNY_IA

pytestmark = pytest.mark.anyio


def families(store, clock, uid, count=2):
    return [store.create_family(uid, "c1", "Claude", False, "[]", clock() + 1000, 10)[0] for _ in range(count)]


async def test_annuler_achat(service, fake):
    bought = await service.enregistrer_achat(JOHNNY, "lait", 2)
    assert fake.amount(1) == 4
    fake.calls.clear()
    out = await service.annuler_derniere_action(JOHNNY)
    assert out["statut"] == "ok" and out["outil_annule"] == "enregistrer_achat"
    assert fake.amount(1) == 2
    tx = bought["transaction_id"]
    assert fake.paths() == [("GET", f"/api/stock/transactions/{tx}"), ("POST", f"/api/stock/transactions/{tx}/undo")]
    assert (await service.annuler_derniere_action(JOHNNY))["statut"] == "rien_a_annuler"


async def test_annuler_seulement_la_derniere_et_la_sienne(service, fake):
    await service.consommer(JOHNNY, "lait", 1)
    await service.enregistrer_achat(ELLI, "beurre", 1)
    assert (await service.annuler_derniere_action(ELLI))["statut"] == "ok"
    assert fake.amount(4) == 1
    assert (await service.annuler_derniere_action(ELLI))["statut"] == "rien_a_annuler"
    assert (await service.annuler_derniere_action(JOHNNY))["statut"] == "ok"
    assert fake.amount(1) == 2


async def test_annuler_apres_24_heures(service, clock):
    await service.consommer(JOHNNY, "lait", 1)
    clock.advance(86_401)
    assert (await service.annuler_derniere_action(JOHNNY))["statut"] == "rien_a_annuler"


async def test_annuler_ignore_les_transactions_hors_mcp(service, fake, settings):
    await GrocyClient(settings.grocy_base_url, KEY_JOHNNY_IA, transport=fake.transport()).purchase(1, 1)
    assert (await service.annuler_derniere_action(JOHNNY))["statut"] == "rien_a_annuler"
    assert fake.amount(1) == 3


async def test_annuler_quand_le_stock_a_change(service, fake):
    await service.enregistrer_achat(JOHNNY, "lait", 2)
    fake.entries[-1]["amount"] -= 1  # consommation faite ailleurs entre-temps
    with pytest.raises(ToolFailure, match="Annulation impossible"):
        await service.annuler_derniere_action(JOHNNY)


async def test_annuler_verifie_le_proprietaire(service, fake):
    await service.consommer(JOHNNY, "lait", 1)
    for row in fake.log:
        row["user_id"] = 4
    with pytest.raises(ToolFailure, match="n'appartient pas"):
        await service.annuler_derniere_action(JOHNNY)


async def test_annuler_deja_annule_dans_grocy(service, fake):
    await service.consommer(JOHNNY, "lait", 1)
    for row in fake.log:
        row["undone"] = 1
    assert (await service.annuler_derniere_action(JOHNNY))["statut"] == "deja_annule"


async def test_cle_grocy_supprimee_revoque_le_compte(service, fake, store, clock, settings):
    johnny = families(store, clock, 3)
    elli = families(store, clock, 4)
    del fake.users[KEY_JOHNNY_IA]
    with pytest.raises(ToolFailure, match="révoquée"):
        await service.voir_stock(JOHNNY)
    assert store.list_active_families(3) == []
    assert {f.id for f in store.list_active_families(4)} == set(elli)
    assert all(store.get_family(f).revoked_at for f in johnny)
    events = [json.loads(line)["event"] for line in settings.audit_path.read_text().splitlines()]
    assert "revocation_cle_grocy" in events
    assert KEY_JOHNNY_IA not in settings.audit_path.read_text()


async def test_un_seul_401_ne_revoque_rien(service, fake, store, clock):
    families(store, clock, 3)
    fake.next_failure = 401
    with pytest.raises(ToolFailure, match="Réessayez"):
        await service.voir_stock(JOHNNY)
    assert len(store.list_active_families(3)) == 2


@pytest.mark.parametrize("failure", [500, 502, 503, "timeout"])
async def test_panne_grocy_ne_revoque_rien(service, fake, store, clock, failure):
    families(store, clock, 3)
    fake.next_failure = failure
    with pytest.raises(ToolFailure, match="ne répond pas"):
        await service.consommer(JOHNNY, "lait", 1)
    assert len(store.list_active_families(3)) == 2
    assert fake.amount(1) == 2


async def test_isolation_entre_comptes(service, fake):
    await service.enregistrer_achat(JOHNNY, "lait", 1)
    await service.enregistrer_achat(ELLI, "beurre", 1)
    users = {(row["product_id"], row["user_id"]) for row in fake.log}
    assert users == {(1, 3), (4, 4)}
