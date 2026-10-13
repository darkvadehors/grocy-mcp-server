"""Client de l'API Grocy : liste fermée de points d'API, clé transmise uniquement dans l'en-tête GROCY-API-KEY."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any

import httpx2

_KEY = re.compile(r"[A-Za-z0-9]{20,100}")
_BARCODE = re.compile(r"[0-9]{8,14}")
_TRANSACTION = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class GrocyError(Exception):
    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class GrocyAuthError(GrocyError):
    """Clé refusée par Grocy (HTTP 401)."""


class GrocyForbidden(GrocyError):
    """Permission Grocy manquante (HTTP 403)."""


class GrocyUnavailable(GrocyError):
    """Grocy injoignable, trop lent ou en erreur serveur."""


class GrocyRequestError(GrocyError):
    """Requête refusée par Grocy (HTTP 4xx autre que 401/403)."""


class InvalidKeyFormat(ValueError):
    pass


def normalize_key(raw: str) -> str:
    key = raw.strip()
    if not _KEY.fullmatch(key):
        raise InvalidKeyFormat("format de clé invalide")
    return key


@dataclass(frozen=True)
class GrocyUser:
    id: int
    username: str


class KeyCheck(enum.Enum):
    OK = "ok"
    INVALID = "invalide"
    ADMIN = "administrateur"
    UNAVAILABLE = "indisponible"


def _int_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("identifiant entier attendu")
    return value


def _error_message(response: httpx2.Response) -> str:
    try:
        message = response.json().get("error_message", "")
    except (ValueError, AttributeError):
        message = ""
    return str(message)[:300]


class GrocyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url
        self._key = normalize_key(api_key)
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"GrocyClient({self._base_url!r}, <clé masquée>)"

    async def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        if not path.startswith("/api/"):
            raise ValueError("chemin hors de l'API Grocy")
        headers = {"GROCY-API-KEY": self._key, "Accept": "application/json"}
        async with httpx2.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
            headers=headers,
        ) as http:
            try:
                response = await http.request(method, path, params=params, json=body)
            except httpx2.HTTPError:
                raise GrocyUnavailable("Grocy ne répond pas") from None
        status = response.status_code
        if status == 401:
            raise GrocyAuthError("clé refusée")
        if status == 403:
            raise GrocyForbidden(_error_message(response))
        if status >= 500 or 300 <= status < 400:
            raise GrocyUnavailable(f"Grocy a répondu {status}")
        if status >= 400:
            raise GrocyRequestError(_error_message(response) or f"Grocy a répondu {status}")
        if status == 204 or not response.content:
            return None
        return response.json()

    # --- identité --------------------------------------------------------------------------------
    async def current_user(self) -> GrocyUser:
        data = await self._request("GET", "/api/user")
        row = data[0] if isinstance(data, list) else data
        return GrocyUser(id=int(row["id"]), username=str(row["username"]))

    async def list_users(self) -> None:
        await self._request("GET", "/api/users")

    # --- données de base (permission Grocy MASTER_DATA_EDIT) -------------------------------------
    _CREATABLE = frozenset({"products", "product_barcodes", "locations", "product_groups"})

    async def external_lookup(self, code: str, add: bool = False) -> dict | None:
        """Recherche du plugin multi-sources ; add=True crée le produit et son code-barres dans Grocy."""
        if not _BARCODE.fullmatch(code):
            raise GrocyRequestError("code-barres invalide (8 à 14 chiffres)")
        params = {"add": "true"} if add else None
        result = await self._request("GET", f"/api/stock/barcodes/external-lookup/{code}", params=params)
        return result if isinstance(result, dict) else None

    async def product_groups(self) -> list[dict]:
        return await self._request("GET", "/api/objects/product_groups")

    async def create_object(self, entity: str, data: dict) -> int:
        if entity not in self._CREATABLE:
            raise ValueError(f"création non autorisée : {entity}")
        result = await self._request("POST", f"/api/objects/{entity}", body=data)
        return _int_id(result["created_object_id"])

    # --- lectures --------------------------------------------------------------------------------
    async def products(self) -> list[dict]:
        return await self._request("GET", "/api/objects/products")

    async def product_by_barcode(self, code: str) -> dict | None:
        if not _BARCODE.fullmatch(code):
            raise GrocyRequestError("code-barres invalide (8 à 14 chiffres)")
        try:
            return await self._request("GET", f"/api/stock/products/by-barcode/{code}")
        except GrocyRequestError as error:
            if "No product with barcode" in error.message:
                return None
            raise

    async def stock(self) -> list[dict]:
        return await self._request("GET", "/api/stock")

    async def volatile(self, due_soon_days: int) -> dict:
        return await self._request("GET", "/api/stock/volatile", params={"due_soon_days": _int_id(due_soon_days)})

    async def product_details(self, product_id: int) -> dict:
        return await self._request("GET", f"/api/stock/products/{_int_id(product_id)}")

    async def product_entries(self, product_id: int) -> list[dict]:
        return await self._request("GET", f"/api/stock/products/{_int_id(product_id)}/entries")

    async def price_history(self, product_id: int) -> list[dict]:
        return await self._request("GET", f"/api/stock/products/{_int_id(product_id)}/price-history")

    async def shopping_list(self) -> list[dict]:
        return await self._request("GET", "/api/objects/shopping_list")

    async def locations(self) -> list[dict]:
        return await self._request("GET", "/api/objects/locations")

    async def quantity_units(self) -> list[dict]:
        return await self._request("GET", "/api/objects/quantity_units")

    async def recipes(self) -> list[dict]:
        return await self._request("GET", "/api/objects/recipes")

    async def recipes_fulfillment(self) -> list[dict]:
        return await self._request("GET", "/api/recipes/fulfillment")

    async def transaction(self, transaction_id: str) -> list[dict]:
        if not _TRANSACTION.fullmatch(transaction_id):
            raise GrocyRequestError("identifiant de transaction invalide")
        return await self._request("GET", f"/api/stock/transactions/{transaction_id}")

    # --- écritures -------------------------------------------------------------------------------
    async def add_to_shopping_list(self, product_id: int, amount: float, note: str | None) -> None:
        body: dict = {"product_id": _int_id(product_id), "list_id": 1, "product_amount": amount}
        if note:
            body["note"] = note
        await self._request("POST", "/api/stock/shoppinglist/add-product", body=body)

    async def remove_from_shopping_list(self, product_id: int, amount: float) -> None:
        body = {"product_id": _int_id(product_id), "list_id": 1, "product_amount": amount}
        await self._request("POST", "/api/stock/shoppinglist/remove-product", body=body)

    async def add_missing_products(self) -> None:
        await self._request("POST", "/api/stock/shoppinglist/add-missing-products", body={"list_id": 1})

    async def add_recipe_missing_products(self, recipe_id: int) -> None:
        await self._request(
            "POST", f"/api/recipes/{_int_id(recipe_id)}/add-not-fulfilled-products-to-shoppinglist", body={}
        )

    async def purchase(
        self,
        product_id: int,
        amount: float,
        price: float | None = None,
        best_before_date: str | None = None,
        location_id: int | None = None,
    ) -> list[dict]:
        body: dict = {"amount": amount, "transaction_type": "purchase"}
        if price is not None:
            body["price"] = price
        if best_before_date is not None:
            body["best_before_date"] = best_before_date
        if location_id is not None:
            body["location_id"] = _int_id(location_id)
        return await self._request("POST", f"/api/stock/products/{_int_id(product_id)}/add", body=body)

    async def consume(self, product_id: int, amount: float, spoiled: bool = False) -> list[dict]:
        body = {"amount": amount, "spoiled": spoiled}
        return await self._request("POST", f"/api/stock/products/{_int_id(product_id)}/consume", body=body)

    async def open_product(self, product_id: int, amount: float) -> list[dict]:
        return await self._request("POST", f"/api/stock/products/{_int_id(product_id)}/open", body={"amount": amount})

    async def transfer(self, product_id: int, amount: float, location_from: int, location_to: int) -> list[dict]:
        body = {"amount": amount, "location_id_from": _int_id(location_from), "location_id_to": _int_id(location_to)}
        return await self._request("POST", f"/api/stock/products/{_int_id(product_id)}/transfer", body=body)

    async def inventory(self, product_id: int, new_amount: float) -> list[dict]:
        body = {"new_amount": new_amount}
        return await self._request("POST", f"/api/stock/products/{_int_id(product_id)}/inventory", body=body)

    async def undo(self, transaction_id: str) -> None:
        if not _TRANSACTION.fullmatch(transaction_id):
            raise GrocyRequestError("identifiant de transaction invalide")
        await self._request("POST", f"/api/stock/transactions/{transaction_id}/undo")


async def check_key(client: GrocyClient) -> tuple[KeyCheck, GrocyUser | None]:
    """Identifie le compte de la clé et refuse les comptes capables de lire les utilisateurs (administrateurs)."""
    try:
        user = await client.current_user()
    except GrocyAuthError:
        return KeyCheck.INVALID, None
    except (GrocyError, KeyError, ValueError, TypeError, IndexError):
        return KeyCheck.UNAVAILABLE, None
    try:
        await client.list_users()
    except GrocyForbidden:
        return KeyCheck.OK, user
    except GrocyAuthError:
        return KeyCheck.INVALID, None
    except GrocyError:
        return KeyCheck.UNAVAILABLE, None
    return KeyCheck.ADMIN, user
