"""In-memory stand-ins for the two services the scraper talks to.

``FakeStorage`` replaces ``StorageClient``. ``FakeKRuoka`` replaces the HTTP
session of a ``KRuokaScraper`` (GraphQL and kr-api), so the scraper's own
request, parsing and error handling run for real. The ``world`` fixture in
``conftest.py`` wires both in.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import requests

from scraper.storage_client import StorageAPIError


class FakeStorage:
    """Holds products, barcodes, the barcode queue, stores, stock and
    availability, and records every call as ``(method, args, kwargs)``."""

    def __init__(self) -> None:
        self.products: dict[int, dict] = {}
        self.barcodes: dict[str, int] = {}
        self.queue: list[dict] = []
        self.stores: dict[str, str] = {}
        self.stock: dict[int, float] = {}
        self.availability: dict[int, list[dict]] = {}
        self.images: dict[str, bytes] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        # Method name -> exception raised by every call to it.
        self.failing: dict[str, Exception] = {}

    # -- test helpers ------------------------------------------------------

    def add_product(self, name: str, barcode: str = "", **fields) -> int:
        product_id = max(self.products, default=0) + 1
        self.products[product_id] = {
            "id": product_id, "name": name, "description": "",
            "picture_filename": None, "unit_price": None, **fields,
        }
        if barcode:
            self.barcodes[barcode] = product_id
        return product_id

    def enqueue(self, barcode: str, **fields) -> dict:
        item = {"id": len(self.queue) + 1, "barcode": barcode,
                "status": "pending", **fields}
        self.queue.append(item)
        return item

    def called(self, method: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for m, a, k in self.calls if m == method]

    def _call(self, method: str, *args, **kwargs) -> None:
        self.calls.append((method, args, kwargs))
        if method in self.failing:
            raise self.failing[method]

    # -- the StorageClient methods the scraper uses --------------------------

    def get_product_by_barcode(self, barcode):
        self._call("get_product_by_barcode", barcode)
        product_id = self.barcodes.get(barcode)
        return dict(self.products[product_id]) if product_id else None

    def create_product(self, name, description="", location_id=None,
                       unit_id=None, **kwargs):
        self._call("create_product", name, description=description,
                   location_id=location_id, unit_id=unit_id, **kwargs)
        return self.add_product(name, description=description, **kwargs)

    def update_product(self, product_id, **fields):
        self._call("update_product", product_id, **fields)
        self.products[product_id].update(fields)

    def delete_product(self, product_id):
        self._call("delete_product", product_id)
        del self.products[product_id]
        self.barcodes = {b: p for b, p in self.barcodes.items() if p != product_id}

    def get_all_products(self, active_only=False):
        self._call("get_all_products")
        return [dict(p) for p in self.products.values()]

    def add_barcode(self, product_id, barcode, pack_size=1.0, pack_unit_id=None):
        self._call("add_barcode", product_id, barcode)
        if barcode in self.barcodes:
            raise StorageAPIError(f"Barcode {barcode} already exists")
        self.barcodes[barcode] = product_id

    def get_all_barcodes(self):
        self._call("get_all_barcodes")
        return [{"product_id": p, "barcode": b} for b, p in self.barcodes.items()]

    def upsert_store(self, store_id, name):
        self._call("upsert_store", store_id, name)
        self.stores[store_id] = name

    def set_product_availability(self, product_id, entries):
        self._call("set_product_availability", product_id, entries)
        self.availability[product_id] = entries

    def get_barcode_queue(self, status="pending"):
        self._call("get_barcode_queue", status=status)
        return [dict(i) for i in self.queue if i["status"] == status]

    def update_barcode_queue_item(self, item_id, *, status=None,
                                  result_product_id=None, error_message=None):
        self._call("update_barcode_queue_item", item_id, status=status,
                   result_product_id=result_product_id, error_message=error_message)
        item = next(i for i in self.queue if i["id"] == item_id)
        for key, value in (("status", status),
                           ("result_product_id", result_product_id),
                           ("error_message", error_message)):
            if value is not None:
                item[key] = value

    def upload_product_image(self, product_id, filename, image_bytes,
                             content_type="application/octet-stream"):
        self._call("upload_product_image", product_id, filename)
        self.images[filename] = image_bytes
        self.products[product_id]["picture_filename"] = filename

    def add_stock(self, product_id, amount=1.0, location_id=None):
        self._call("add_stock", product_id, amount=amount)
        self.stock[product_id] = self.stock.get(product_id, 0.0) + amount


class _Response:
    def __init__(self, status: int, payload=None) -> None:
        self.status_code = status
        self.headers: dict = {}
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)

    def json(self):
        return self._payload


class FakeKRuoka:
    """K-Ruoka as one session object: the mobile GraphQL API and kr-api.

    ``catalogue`` maps a store id to ``{ean: name}``. ``prices`` maps an EAN
    to its kr-api price, ``store_names`` a store id to its kr-api name.
    ``down`` makes GraphQL unreachable; ``krapi_status`` is the HTTP status
    kr-api answers with. Every request is logged as
    ``(api, store_id, query)``.
    """

    def __init__(self, catalogue=None, prices=None, store_names=None) -> None:
        self.catalogue: dict[str, dict[str, str]] = catalogue or {}
        self.prices: dict[str, float] = prices or {}
        self.store_names: dict[str, str] = store_names or {}
        self.down = False
        self.krapi_status = 200
        self.requests: list[tuple[str, str, str]] = []
        self.headers: dict = {}

    def asked(self, api: str) -> list[tuple[str, str]]:
        return [(store, query) for a, store, query in self.requests if a == api]

    def _hits(self, store_id: str, query: str) -> list[tuple[str, str]]:
        shelf = self.catalogue.get(store_id, {})
        if query.isdigit():
            return [(query, shelf[query])] if query in shelf else []
        return [(ean, name) for ean, name in shelf.items()
                if query.lower() in name.lower()]

    def post(self, url, json=None, timeout=None):
        if "graphql" in url:
            variables = json["variables"]
            store_id, query = variables["storeId"], variables["query"]
            self.requests.append(("graphql", store_id, query))
            if self.down:
                raise requests.ConnectionError("k-ruoka.fi unreachable")
            hits = self._hits(store_id, query) if not variables["offset"] else []
            return _Response(200, {"data": {"productAndAssortmentSearchV2": {
                "results": [
                    {"__typename": "Product", "id": ean, "ean": ean,
                     "localizedName": {"finnish": name},
                     "imageUrl": f"https://img.example/{ean}"}
                    for ean, name in hits
                ],
                "totalHits": len(hits),
            }}})

        # kr-api: .../kr-api/v2/product-search/{query}?storeId=...
        parsed = urlparse(url)
        query = unquote(parsed.path.rsplit("/", 1)[-1])
        params = parse_qs(parsed.query)
        store_id = params["storeId"][0]
        self.requests.append(("krapi-search", store_id, query))
        if self.krapi_status != 200:
            return _Response(self.krapi_status)
        hits = self._hits(store_id, query) if params["offset"] == ["0"] else []
        return _Response(200, {"result": [
            {"id": ean, "product": {
                "ean": ean, "localizedName": {"finnish": name},
                **({"mobilescan": {"pricing": {"normal": {"price": self.prices[ean]}}}}
                   if ean in self.prices else {}),
            }}
            for ean, name in hits
        ], "totalHits": len(hits)})

    def get(self, url, timeout=None):
        # kr-api: .../kr-api/store/{id}
        store_id = url.rsplit("/", 1)[-1]
        self.requests.append(("krapi-store", store_id, ""))
        if self.krapi_status != 200:
            return _Response(self.krapi_status)
        if store_id not in self.store_names:
            return _Response(404)
        return _Response(200, {"name": self.store_names[store_id]})
