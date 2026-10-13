"""Grocy 4.7.1 simulé en mémoire (comportements relevés dans le code source de Grocy 4.7.1)."""

from __future__ import annotations

import copy
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx2

KEY_JOHNNY_IA = "jia" + "1" * 47
KEY_ELLI_IA = "eia" + "2" * 47
KEY_ADMIN = "adm" + "3" * 47
TODAY = date(2026, 10, 13)
IA_PERMISSIONS = frozenset(
    {
        "STOCK_PURCHASE",
        "STOCK_CONSUME",
        "STOCK_INVENTORY",
        "STOCK_TRANSFER",
        "STOCK_OPEN",
        "STOCK_EDIT",
        "SHOPPINGLIST_ITEMS_ADD",
        "SHOPPINGLIST_ITEMS_DELETE",
        "RECIPES",
        "MASTER_DATA_EDIT",
    }
)


@dataclass
class FakeUser:
    id: int
    username: str
    admin: bool = False
    permissions: set[str] = field(default_factory=lambda: set(IA_PERMISSIONS))

    def can(self, permission: str) -> bool:
        return self.admin or permission in self.permissions


def _err(status: int, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={"error_message": message})


class FakeGrocy:
    def __init__(self) -> None:
        self.users = {
            KEY_JOHNNY_IA: FakeUser(3, "johnny-ia"),
            KEY_ELLI_IA: FakeUser(4, "elli-ia"),
            KEY_ADMIN: FakeUser(1, "johnny", admin=True),
        }
        self.products = {
            1: {"id": 1, "name": "Lait demi-écrémé", "location_id": 1, "qu_id_stock": 1, "active": 1},
            2: {"id": 2, "name": "Yaourt nature", "location_id": 1, "qu_id_stock": 1, "active": 1},
            3: {"id": 3, "name": "Yaourt fraise", "location_id": 1, "qu_id_stock": 1, "active": 1},
            4: {"id": 4, "name": "Beurre doux", "location_id": 1, "qu_id_stock": 1, "active": 1},
            5: {"id": 5, "name": "Pâtes", "location_id": 2, "qu_id_stock": 1, "active": 1},
        }
        self.barcodes = {"3017620422003": 1}
        self.locations = [{"id": 1, "name": "Frigo"}, {"id": 2, "name": "Placard"}]
        self.quantity_units = [{"id": 1, "name": "Pièce", "name_plural": "Pièces"}]
        self.product_groups: list[dict] = []
        # Champs personnalisés des produits (créés en production par grocy_nutrition_fields.py)
        self.userfield_names = {
            "nutri_kcal_100g", "nutri_proteines_100g", "nutri_glucides_100g", "nutri_sucres_100g", "nutri_lipides_100g",
            "nutri_ags_100g", "nutri_fibres_100g", "nutri_sel_100g", "nutri_poids_unite_g", "nutri_source",
            "nutri_non_alimentaire",
        }
        self.userfield_values: dict[int, dict[str, str]] = {}
        self.lookup_db: dict[str, dict] = {}  # code-barres → proposition du plugin multi-sources
        self.lookups: list[tuple[str, bool]] = []  # (code, add) dans l'ordre des appels
        self.recipes = [{"id": 1, "name": "Crêpes"}]
        self.fulfillment = [
            {"recipe_id": 1, "need_fulfilled": 0, "missing_products_count": 1, "product_names_comma_separated": "Beurre doux"}
        ]
        self.missing = [{"id": 4, "name": "Beurre doux", "amount_missing": 1}]
        self.entries: list[dict] = []
        self.log: list[dict] = []
        self.shopping: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.next_failure: int | str | None = None
        self.timeout_after_next_write = False  # Grocy enregistre l'écriture puis la réponse se perd
        self._ids = itertools.count(100)
        self._tx = itertools.count(1)
        self._snapshots: dict[str, tuple[list[dict], list[dict], set[int]]] = {}
        self.add_entry(1, 2, TODAY + timedelta(days=2), 1)
        self.add_entry(2, 4, TODAY + timedelta(days=7), 1)
        self.add_entry(3, 1, TODAY - timedelta(days=2), 1)
        self.add_entry(4, 1, TODAY + timedelta(days=80), 1)

    # --- outils de test ---------------------------------------------------------------------------
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def paths(self) -> list[tuple[str, str]]:
        return list(self.calls)

    def add_entry(self, product_id: int, amount: float, best_before: date, location_id: int) -> dict:
        entry = {
            "stock_id": f"s{next(self._ids)}",
            "product_id": product_id,
            "amount": float(amount),
            "amount_opened": 0.0,
            "best_before_date": best_before.isoformat(),
            "location_id": location_id,
            "price": None,
        }
        self.entries.append(entry)
        return entry

    def amount(self, product_id: int) -> float:
        return sum(e["amount"] for e in self.entries if e["product_id"] == product_id)

    # --- routeur ----------------------------------------------------------------------------------
    def handle(self, request: httpx2.Request) -> httpx2.Response:
        response = self._handle(request)
        if self.timeout_after_next_write and request.method == "POST":
            self.timeout_after_next_write = False
            raise httpx2.ReadTimeout("délai dépassé après l'écriture", request=request)
        return response

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append((request.method, request.url.path))
        if self.next_failure is not None:
            failure, self.next_failure = self.next_failure, None
            if failure == "timeout":
                raise httpx2.ReadTimeout("délai dépassé", request=request)
            return httpx2.Response(int(failure))
        user = self.users.get(request.headers.get("GROCY-API-KEY", ""))
        if user is None:
            return httpx2.Response(401)
        path, method = request.url.path, request.method
        body: dict = {}
        if method in ("POST", "PUT") and not path.endswith("/undo"):
            if request.headers.get("content-type") != "application/json":
                return _err(400, "Bad Content-Type")
            body = json.loads(request.content or b"{}")
        for pattern, verb, handler in self._routes():
            m = re.fullmatch(pattern, path)
            if m and verb == method:
                return handler(user, request, body, *m.groups())
        return _err(404, "route inconnue du simulateur")

    def _routes(self):
        return [
            (r"/api/user", "GET", self._user),
            (r"/api/users", "GET", self._users),
            (r"/api/objects/(products|shopping_list|locations|quantity_units|recipes|product_groups|stock|product_barcodes)", "GET", self._objects),
            (r"/api/objects/(products|product_barcodes|locations|product_groups)", "POST", self._create_object),
            (r"/api/stock/barcodes/external-lookup/([^/]+)", "GET", self._external_lookup),
            (r"/api/userfields/products/(\d+)", "GET", self._get_userfields),
            (r"/api/userfields/products/(\d+)", "PUT", self._put_userfields),
            (r"/api/objects/products/(\d+)", "PUT", self._put_product),
            (r"/api/stock", "GET", self._stock),
            (r"/api/stock/volatile", "GET", self._volatile),
            (r"/api/stock/products/by-barcode/([^/]+)", "GET", self._by_barcode),
            (r"/api/stock/products/(\d+)", "GET", self._details),
            (r"/api/stock/products/(\d+)/entries", "GET", self._entries),
            (r"/api/stock/products/(\d+)/price-history", "GET", self._price_history),
            (r"/api/stock/products/(\d+)/(add|consume|open|transfer|inventory)", "POST", self._stock_action),
            (r"/api/stock/transactions/([^/]+)", "GET", self._transaction),
            (r"/api/stock/transactions/([^/]+)/undo", "POST", self._undo),
            (r"/api/stock/shoppinglist/(add-product|remove-product|add-missing-products)", "POST", self._shopping),
            (r"/api/recipes/(\d+)/add-not-fulfilled-products-to-shoppinglist", "POST", self._recipe_missing),
            (r"/api/recipes/fulfillment", "GET", self._fulfillment),
        ]

    # --- lectures ---------------------------------------------------------------------------------
    def _user(self, user, *_):
        return httpx2.Response(200, json=[{"id": user.id, "username": user.username, "display_name": user.username}])

    def _users(self, user, *_):
        if not user.can("USERS_READ"):
            return _err(403, "Permission missing: USERS_READ")
        return httpx2.Response(200, json=[{"id": u.id, "username": u.username} for u in self.users.values()])

    def _objects(self, user, request, body, entity):
        data = {
            "products": [{**p, "userfields": self._userfields_of(p["id"])} for p in self.products.values()],
            "shopping_list": self.shopping,
            "locations": self.locations,
            "quantity_units": self.quantity_units,
            "recipes": self.recipes,
            "product_groups": self.product_groups,
            "stock": [{**e, "amount": str(e["amount"])} for e in self.entries],  # Grocy : montants en texte
            "product_barcodes": [{"product_id": pid, "barcode": code} for code, pid in self.barcodes.items()],
        }[entity]
        return httpx2.Response(200, json=data)

    # --- champs personnalisés (Grocy 4.7.1 : valeurs en texte ; lecture sans permission) -------------
    def _userfields_of(self, product_id: int) -> dict:
        values = self.userfield_values.get(int(product_id), {})
        return {name: values.get(name) for name in sorted(self.userfield_names)}

    def _get_userfields(self, user, request, body, product_id):
        return httpx2.Response(200, json=self._userfields_of(int(product_id)))

    def _put_userfields(self, user, request, body, product_id):
        if not user.can("MASTER_DATA_EDIT"):
            return _err(403, "Permission missing: MASTER_DATA_EDIT")
        unknown = set(body) - self.userfield_names
        if unknown:
            return _err(400, f"Field {sorted(unknown)[0]} is not a valid userfield of the given entity")
        values = self.userfield_values.setdefault(int(product_id), {})
        values.update({k: (None if v is None else str(v)) for k, v in body.items()})
        return httpx2.Response(204)

    def _put_product(self, user, request, body, product_id):
        if not user.can("MASTER_DATA_EDIT"):
            return _err(403, "Permission missing: MASTER_DATA_EDIT")
        if int(product_id) not in self.products:
            return _err(400, "Object not found")
        self.products[int(product_id)].update(body)
        return httpx2.Response(204)

    # --- données de base (Grocy 4.7.1 : MASTER_DATA_EDIT, noms uniques) ---------------------------
    def _insert(self, entity: str, data: dict) -> tuple[int | None, httpx2.Response | None]:
        if entity == "product_barcodes":
            if data.get("barcode") in self.barcodes:
                return None, _err(400, "UNIQUE constraint failed: product_barcodes.barcode")
            if int(data.get("product_id", 0)) not in self.products:
                return None, _err(400, "product_id does not reference an existing product")
            self.barcodes[str(data["barcode"])] = int(data["product_id"])
            return int(data["product_id"]), None
        table = {"products": list(self.products.values()), "locations": self.locations, "product_groups": self.product_groups}[entity]
        name = data.get("name")
        if not name:
            return None, _err(400, f"NOT NULL constraint failed: {entity}.name")
        if any(row["name"] == name for row in table):
            return None, _err(400, f"UNIQUE constraint failed: {entity}.name")
        new_id = next(self._ids)
        row = {**data, "id": new_id, "active": 1}
        if entity == "products":
            for field_name in ("location_id", "qu_id_purchase", "qu_id_stock"):
                if field_name not in data:
                    return None, _err(400, f"NOT NULL constraint failed: products.{field_name}")
            self.products[new_id] = row
        else:
            table.append(row)
        return new_id, None

    def _create_object(self, user, request, body, entity):
        if not user.can("MASTER_DATA_EDIT"):
            return _err(403, "Permission missing: MASTER_DATA_EDIT")
        new_id, error = self._insert(entity, body)
        # Grocy 4.7.1 renvoie lastInsertId() de PDO : une chaîne, pas un nombre
        return error or httpx2.Response(200, json={"created_object_id": str(new_id)})

    def _external_lookup(self, user, request, body, code):
        if not user.can("MASTER_DATA_EDIT"):
            return _err(403, "Permission missing: MASTER_DATA_EDIT")
        add = request.url.params.get("add") in ("true", "1")
        self.lookups.append((code, add))
        found = self.lookup_db.get(code)
        if found is None:
            return httpx2.Response(200, json=None)
        proposal = {"location_id": 1, "qu_id_purchase": 1, "qu_id_stock": 1, **found, "__barcode": code}
        if not add:
            return httpx2.Response(200, json=proposal)
        data = {k: v for k, v in proposal.items() if not k.startswith("__")}
        new_id, error = self._insert("products", data)
        if error:
            return error
        _, error = self._insert("product_barcodes", {"product_id": new_id, "barcode": code})  # Grocy : produit déjà créé
        if error:
            return error
        return httpx2.Response(200, json={**proposal, "id": new_id})

    def _grouped(self, entries: list[dict]) -> list[dict]:
        rows: dict[int, dict] = {}
        for e in entries:
            row = rows.setdefault(
                e["product_id"],
                {
                    "product_id": e["product_id"],
                    "amount": 0.0,
                    "amount_opened": 0.0,
                    "best_before_date": e["best_before_date"],
                    "product": self.products[e["product_id"]],
                },
            )
            row["amount"] += e["amount"]
            row["amount_opened"] += e["amount_opened"]
            row["best_before_date"] = min(row["best_before_date"], e["best_before_date"])
        return [r for r in rows.values() if r["amount"] > 0]

    def _stock(self, *_):
        return httpx2.Response(200, json=self._grouped(self.entries))

    def _volatile(self, user, request, *_):
        days = int(request.url.params.get("due_soon_days", "5"))
        limit = (TODAY + timedelta(days=days)).isoformat()
        today = TODAY.isoformat()
        due = [e for e in self.entries if today <= e["best_before_date"] <= limit]
        overdue = [e for e in self.entries if e["best_before_date"] < today]
        return httpx2.Response(
            200,
            json={
                "due_products": self._grouped(due),
                "overdue_products": self._grouped(overdue),
                "expired_products": [],
                "missing_products": self.missing,
            },
        )

    def _product_or_error(self, product_id: int):
        product = self.products.get(product_id)
        if product is None:
            return None, _err(400, "Product does not exist or is inactive")
        return product, None

    def _details_for(self, product_id: int) -> httpx2.Response:
        product, error = self._product_or_error(product_id)
        if error:
            return error
        mine = [e for e in self.entries if e["product_id"] == product_id and e["amount"] > 0]
        location = next(loc for loc in self.locations if loc["id"] == product["location_id"])
        return httpx2.Response(
            200,
            json={
                "product": product,
                "stock_amount": self.amount(product_id),
                "stock_amount_opened": sum(e["amount_opened"] for e in mine),
                "next_due_date": min((e["best_before_date"] for e in mine), default=None),
                "location": location,
                "quantity_unit_stock": self.quantity_units[0],
                "last_price": None,
            },
        )

    def _details(self, user, request, body, product_id):
        return self._details_for(int(product_id))

    def _by_barcode(self, user, request, body, code):
        product_id = self.barcodes.get(code)
        if product_id is None:
            return _err(400, f"No product with barcode {code} found")
        return self._details_for(product_id)

    def _entries(self, user, request, body, product_id):
        return httpx2.Response(200, json=[e for e in self.entries if e["product_id"] == int(product_id)])

    def _price_history(self, user, request, body, product_id):
        _, error = self._product_or_error(int(product_id))
        return error or httpx2.Response(200, json=[])

    def _fulfillment(self, *_):
        return httpx2.Response(200, json=self.fulfillment)

    def _transaction(self, user, request, body, transaction_id):
        rows = [r for r in self.log if r["transaction_id"] == transaction_id]
        if not rows:
            return _err(400, "No transaction was found by the given transaction id")
        return httpx2.Response(200, json=rows)

    # --- écritures --------------------------------------------------------------------------------
    _PERMISSIONS = {
        "add": "STOCK_PURCHASE",
        "consume": "STOCK_CONSUME",
        "open": "STOCK_OPEN",
        "transfer": "STOCK_TRANSFER",
        "inventory": "STOCK_INVENTORY",
    }

    def _stock_action(self, user, request, body, product_id, action):
        permission = self._PERMISSIONS[action]
        if not user.can(permission):
            return _err(403, f"Permission missing: {permission}")
        pid = int(product_id)
        _, error = self._product_or_error(pid)
        if error:
            return error
        before = copy.deepcopy(self.entries)
        tx = f"tx{next(self._tx)}"
        rows: list[dict] = []

        def log(amount, kind, stock_id):
            rows.append(
                {
                    "id": next(self._ids),
                    "transaction_id": tx,
                    "product_id": pid,
                    "amount": amount,
                    "transaction_type": kind,
                    "stock_id": stock_id,
                    "undone": 0,
                    "user_id": user.id,
                }
            )

        if action == "inventory":
            if "new_amount" not in body:
                return _err(400, "An new amount is required")
            diff = float(body["new_amount"]) - self.amount(pid)
            if diff == 0:
                return _err(400, "No transaction was found by the given transaction id")
            if diff > 0:
                entry = self.add_entry(pid, diff, TODAY + timedelta(days=30), self.products[pid]["location_id"])
                log(diff, "inventory-correction", entry["stock_id"])
            else:
                self._take(pid, -diff, None, lambda e, n: log(-n, "inventory-correction", e["stock_id"]))
        else:
            if "amount" not in body:
                return _err(400, "An amount is required")
            amount = float(body["amount"])
            if amount <= 0:
                return _err(400, "Amount can't be <= 0")
            if action == "add":
                bbd = body.get("best_before_date") or (TODAY + timedelta(days=30)).isoformat()
                entry = self.add_entry(
                    pid, amount, date.fromisoformat(bbd), body.get("location_id") or self.products[pid]["location_id"]
                )
                entry["price"] = body.get("price")
                log(amount, "purchase", entry["stock_id"])
            elif action == "consume":
                if amount > self.amount(pid):
                    return _err(400, "Amount to be consumed cannot be > current stock amount")
                kind = "consume"
                self._take(pid, amount, None, lambda e, n: log(-n, kind, e["stock_id"]))
            elif action == "open":
                unopened = sum(e["amount"] - e["amount_opened"] for e in self.entries if e["product_id"] == pid)
                if amount > unopened:
                    return _err(400, "Amount to be opened cannot be > current unopened stock amount")
                remaining = amount
                for e in self.entries:
                    if e["product_id"] == pid and remaining > 0:
                        n = min(remaining, e["amount"] - e["amount_opened"])
                        e["amount_opened"] += n
                        remaining -= n
                        if n:
                            log(n, "product-opened", e["stock_id"])
            elif action == "transfer":
                ids = {loc["id"] for loc in self.locations}
                src, dst = body.get("location_id_from"), body.get("location_id_to")
                if src not in ids:
                    return _err(400, "Source location does not exist")
                if dst not in ids:
                    return _err(400, "Destination location does not exist")
                at_source = sum(e["amount"] for e in self.entries if e["product_id"] == pid and e["location_id"] == src)
                if amount > at_source:
                    return _err(400, "Amount to be transfered cannot be > current stock amount in the source location")

                def move(e, n):
                    moved = self.add_entry(pid, n, date.fromisoformat(e["best_before_date"]), dst)
                    log(-n, "transfer_from", e["stock_id"])
                    log(n, "transfer_to", moved["stock_id"])

                self._take(pid, amount, src, move)
        self.entries = [e for e in self.entries if e["amount"] > 0]
        self.log.extend(rows)
        self._snapshots[tx] = (before, copy.deepcopy(self.entries), {pid})
        return httpx2.Response(200, json=rows)

    def _take(self, pid: int, amount: float, location_id: int | None, on_take) -> None:
        remaining = amount
        for e in sorted(self.entries, key=lambda x: x["best_before_date"]):
            if e["product_id"] != pid or remaining <= 0 or (location_id is not None and e["location_id"] != location_id):
                continue
            n = min(remaining, e["amount"])
            e["amount"] -= n
            remaining -= n
            on_take(e, n)

    def _undo(self, user, request, body, transaction_id):
        if not user.can("STOCK_EDIT"):
            return _err(403, "Permission missing: STOCK_EDIT")
        rows = [r for r in self.log if r["transaction_id"] == transaction_id and not r["undone"]]
        if not rows:
            return _err(400, "This transaction was not found or already undone")
        before, after, products = self._snapshots[transaction_id]

        def only(entries):
            return [e for e in entries if e["product_id"] in products]

        if only(self.entries) != only(after):
            return _err(400, "Booking has subsequent dependent bookings, undo not possible")
        self.entries = [e for e in self.entries if e["product_id"] not in products] + only(before)
        for r in rows:
            r["undone"] = 1
        return httpx2.Response(204)

    def _shopping(self, user, request, body, action):
        permission = "SHOPPINGLIST_ITEMS_DELETE" if action == "remove-product" else "SHOPPINGLIST_ITEMS_ADD"
        if not user.can(permission):
            return _err(403, f"Permission missing: {permission}")
        if action == "add-missing-products":
            for m in self.missing:
                self._add_to_list(m["id"], m["amount_missing"], None)
            return httpx2.Response(204)
        if "product_id" not in body:
            return _err(400, "No product id was supplied")
        pid = int(body["product_id"])
        amount = float(body.get("product_amount", 1))
        if action == "add-product":
            if pid not in self.products:
                return _err(400, "Product does not exist or is inactive")
            self._add_to_list(pid, amount, body.get("note"))
        else:
            for item in list(self.shopping):
                if item["product_id"] == pid:
                    item["amount"] -= amount
                    if item["amount"] <= 0.001:
                        self.shopping.remove(item)
        return httpx2.Response(204)

    def _add_to_list(self, pid: int, amount: float, note: str | None) -> None:
        for item in self.shopping:
            if item["product_id"] == pid:
                item["amount"] += amount
                item["note"] = note
                return
        self.shopping.append(
            {"id": next(self._ids), "product_id": pid, "amount": float(amount), "note": note, "shopping_list_id": 1}
        )

    def _recipe_missing(self, user, request, body, recipe_id):
        if not user.can("SHOPPINGLIST_ITEMS_ADD"):
            return _err(403, "Permission missing: SHOPPINGLIST_ITEMS_ADD")
        if int(recipe_id) not in {r["id"] for r in self.recipes}:
            return _err(500, "Recipe does not exist")
        self._add_to_list(4, 1, None)
        return httpx2.Response(204)
