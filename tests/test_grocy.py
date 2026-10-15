import pytest

from grocy_mcp.grocy import (
    GrocyAuthError,
    GrocyClient,
    GrocyForbidden,
    GrocyRequestError,
    GrocyUnavailable,
    InvalidKeyFormat,
    KeyCheck,
    check_key,
    normalize_key,
)
from tests.fake_grocy import KEY_ADMIN, KEY_ELLI_IA, KEY_JOHNNY_IA

pytestmark = pytest.mark.anyio
BASE = "https://grocy.example.test"


def client(fake, key=KEY_JOHNNY_IA):
    return GrocyClient(BASE, key, transport=fake.transport())


def test_normalize_key():
    assert normalize_key(f"  {KEY_JOHNNY_IA}\n") == KEY_JOHNNY_IA
    for bad in ("", "court", KEY_JOHNNY_IA + "\r\nX-Injecte: 1", "é" * 50, "a" * 101, KEY_JOHNNY_IA[:25] + " " + KEY_JOHNNY_IA[25:]):
        with pytest.raises(InvalidKeyFormat):
            normalize_key(bad)


async def test_current_user_and_headers(fake):
    user = await client(fake).current_user()
    assert (user.id, user.username) == (3, "johnny-ia")
    assert fake.calls == [("GET", "/api/user")]


@pytest.mark.parametrize(
    ("key", "expected", "username"),
    [(KEY_JOHNNY_IA, KeyCheck.OK, "johnny-ia"), (KEY_ELLI_IA, KeyCheck.OK, "elli-ia"), (KEY_ADMIN, KeyCheck.ADMIN, "johnny")],
)
async def test_check_key(fake, key, expected, username):
    outcome, user = await check_key(client(fake, key))
    assert outcome is expected
    assert user.username == username


async def test_check_key_invalid_and_unavailable(fake):
    assert await check_key(client(fake, "x" * 50)) == (KeyCheck.INVALID, None)
    fake.next_failure = 502
    assert await check_key(client(fake)) == (KeyCheck.UNAVAILABLE, None)
    fake.next_failure = "timeout"
    assert await check_key(client(fake)) == (KeyCheck.UNAVAILABLE, None)


async def test_error_mapping(fake):
    c = client(fake)
    with pytest.raises(GrocyAuthError):
        await client(fake, "x" * 50).stock()
    fake.users[KEY_JOHNNY_IA].permissions.discard("STOCK_CONSUME")
    with pytest.raises(GrocyForbidden, match="STOCK_CONSUME"):
        await c.consume(1, 1)
    fake.users[KEY_JOHNNY_IA].permissions.add("STOCK_CONSUME")
    with pytest.raises(GrocyRequestError, match="cannot be >"):
        await c.consume(1, 99)
    fake.next_failure = 500
    with pytest.raises(GrocyUnavailable):
        await c.stock()


async def test_barcode_lookup(fake):
    c = client(fake)
    assert (await c.product_by_barcode("3017620422003"))["product"]["id"] == 1
    assert await c.product_by_barcode("12345678") is None
    with pytest.raises(GrocyRequestError):
        await c.product_by_barcode("../../users")


async def test_actions_return_transaction_rows(fake):
    c = client(fake)
    rows = await c.purchase(1, 2, price=1.5, best_before_date="2026-11-01", location_id=2)
    assert rows[0]["transaction_id"] and rows[0]["user_id"] == 3
    assert fake.amount(1) == 4
    rows = await c.consume(1, 1, spoiled=True)
    tx = rows[0]["transaction_id"]
    assert (await c.transaction(tx))[0]["undone"] == 0
    await c.undo(tx)
    assert fake.amount(1) == 4
    with pytest.raises(GrocyRequestError, match="already undone"):
        await c.undo(tx)
    with pytest.raises(GrocyRequestError):
        await c.undo("../x")


async def test_shopping_list_calls(fake):
    c = client(fake)
    await c.add_to_shopping_list(1, 2, "demi-écrémé")
    await c.remove_from_shopping_list(1, 1)
    await c.add_missing_products()
    await c.add_recipe_missing_products(1)
    assert [(i["product_id"], i["amount"]) for i in await c.shopping_list()] == [(1, 1.0), (4, 2.0)]


async def test_volatile_parameter(fake):
    data = await client(fake).volatile(5)
    assert [r["product_id"] for r in data["due_products"]] == [1]
    assert [r["product_id"] for r in data["overdue_products"]] == [3]


async def test_ids_are_integers(fake):
    with pytest.raises(TypeError):
        await client(fake).product_details("1/../../users")


async def test_external_lookup_preview_and_add(fake):
    c = client(fake)
    assert await c.external_lookup("5449000000996") is None  # inconnu du plugin : 200 avec null
    fake.lookup_db["5449000000996"] = {"name": "Coca-Cola 33 cl", "description": "Source : Open Food Facts"}
    preview = await c.external_lookup("5449000000996")
    assert preview["name"] == "Coca-Cola 33 cl" and "id" not in preview
    assert "5449000000996" not in fake.barcodes  # l'aperçu ne crée rien
    created = await c.external_lookup("5449000000996", add=True)
    assert fake.barcodes["5449000000996"] == created["id"] and fake.products[created["id"]]["name"] == "Coca-Cola 33 cl"
    with pytest.raises(GrocyRequestError):
        await c.external_lookup("12")


async def test_external_lookup_requires_master_data_edit(fake):
    fake.users[KEY_JOHNNY_IA].permissions.discard("MASTER_DATA_EDIT")
    with pytest.raises(GrocyForbidden, match="MASTER_DATA_EDIT"):
        await client(fake).external_lookup("5449000000996")


async def test_create_objects_and_product_groups(fake):
    c = client(fake)
    loc = await c.create_object("locations", {"name": "Cave", "is_freezer": 0})
    assert any(row["id"] == loc and row["name"] == "Cave" for row in await c.locations())
    group = await c.create_object("product_groups", {"name": "Boissons"})
    assert [g["id"] for g in await c.product_groups()] == [group]
    pid = await c.create_object("products", {"name": "Eau", "location_id": loc, "qu_id_purchase": 1, "qu_id_stock": 1})
    await c.create_object("product_barcodes", {"product_id": pid, "barcode": "3274080005003"})
    assert (await c.product_by_barcode("3274080005003"))["product"]["id"] == pid
    with pytest.raises(GrocyRequestError):
        await c.create_object("locations", {"name": "Cave"})  # nom unique


async def test_create_object_closed_list(fake):
    with pytest.raises(ValueError):
        await client(fake).create_object("users", {"username": "x"})


async def test_userfields_lecture_ecriture(fake):
    c = client(fake)
    assert await c.userfields(1) == {}
    await c.set_userfields(1, {"nutri_kcal_100g": 47, "nutri_source": "Ciqual 19041"})
    assert (await c.userfields(1))["nutri_kcal_100g"] == "47"  # Grocy stocke les valeurs en texte
    products = {p["id"]: p for p in await c.products()}
    assert products[1]["userfields"]["nutri_source"] == "Ciqual 19041"
    with pytest.raises(GrocyRequestError):
        await c.set_userfields(1, {"champ_inconnu": 1})


async def test_userfields_ecriture_exige_master_data_edit(fake):
    fake.users[KEY_JOHNNY_IA].permissions.discard("MASTER_DATA_EDIT")
    assert await client(fake).userfields(1) == {}  # lecture sans permission
    with pytest.raises(GrocyForbidden):
        await client(fake).set_userfields(1, {"nutri_kcal_100g": 47})


async def test_calories_seul_champ_modifiable(fake):
    c = client(fake)
    await c.set_product_calories(1, 470.25)
    assert fake.products[1]["calories"] == 470.25 and fake.products[1]["name"] == "Lait demi-écrémé"
    assert [m for m, p in fake.paths() if p == "/api/objects/products/1"] == ["PUT"]
    with pytest.raises(ValueError):
        await c.set_product_calories(1, -5)


async def test_purchase_with_store_and_date(fake):
    c = client(fake)
    rows = await c.purchase(1, 2, price=1.25, purchased_date="2026-10-12", shopping_location_id=2)
    entry = fake.entries[-1]
    assert (entry["price"], entry["purchased_date"], entry["shopping_location_id"]) == (1.25, "2026-10-12", 2)
    assert rows[0]["transaction_type"] == "purchase"


async def test_edit_stock_entry_and_undo(fake):
    c = client(fake)
    entry = fake.entries[0]
    rows = await c.edit_stock_entry(entry["id"], {**{k: entry[k] for k in ("amount", "best_before_date", "location_id",
                                                                           "purchased_date")}, "open": 0, "price": 0.99,
                                                   "shopping_location_id": 2})
    assert {r["transaction_type"] for r in rows} == {"stock-edit-old", "stock-edit-new"}
    assert (entry["price"], entry["shopping_location_id"]) == (0.99, 2)
    await c.undo(rows[0]["transaction_id"])
    assert fake.entries[0]["price"] is None


async def test_shopping_locations_and_stock_log(fake):
    c = client(fake)
    assert [s["name"] for s in await c.shopping_locations()] == ["Leclerc", "Intermarché"]
    new_id = await c.create_object("shopping_locations", {"name": "Grand Frais"})
    assert any(s["id"] == new_id for s in await c.shopping_locations())
    await c.purchase(1, 1, price=2.0, shopping_location_id=2)
    assert [r["price"] for r in await c.stock_log() if r["transaction_type"] == "purchase"] == [2.0]
    history = await c.price_history(1)
    assert history[0]["price"] == 2.0 and history[0]["shopping_location"]["id"] == 2
