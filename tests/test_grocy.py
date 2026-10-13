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
