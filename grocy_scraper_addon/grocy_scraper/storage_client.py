"""HA-Storage REST API client.

Provides the same public interface as :class:`GrocyClient` but talks to the
custom HA-Storage addon (FastAPI) instead of the upstream Grocy API.

Key differences from GrocyClient
---------------------------------
* No API key – the Storage addon runs inside the trusted HA network.
* Created objects return ``{"id": …}`` (not ``created_object_id``).
* File uploads use plain filenames (no Base64 encoding).
* Error responses follow the FastAPI ``{"detail": "…"}`` convention.
* Backward-compatibility aliases are injected for fields that
  ``main.py`` still reads under the old Grocy names (``from_qu_id``,
  ``to_qu_id``, ``qu_id``, ``description``).
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _response_error_detail(response: requests.Response | None) -> str:
    """Extract a human-readable error snippet from a FastAPI error response."""
    if response is None:
        return ""
    try:
        data = response.json()
        detail = data.get("detail") or ""
        if isinstance(detail, list):
            # FastAPI validation errors come as a list of dicts.
            detail = "; ".join(
                d.get("msg", str(d)) for d in detail
            )
        if detail:
            return f" – {detail}"
    except (ValueError, AttributeError):
        pass
    text = response.text.strip()
    if text:
        return f" – {text[:300]}"
    return ""


# ------------------------------------------------------------------
# Exception
# ------------------------------------------------------------------

class StorageAPIError(Exception):
    """Raised when the Storage API returns an unexpected response."""


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------

class StorageClient:
    """Thin HTTP client for the HA-Storage addon.

    Parameters
    ----------
    base_url:
        Root URL of the Storage addon, e.g. ``"http://localhost:8099"``.
    timeout:
        Default request timeout in seconds.
    """

    def __init__(self, base_url: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.timeout = timeout

        # Maps ingredient-id → recipe-id so that update_recipe_position
        # can route to the correct nested endpoint.
        self._ingredient_to_recipe: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def get_product_by_barcode(self, barcode: str) -> Optional[dict]:
        """Return the product dict for *barcode*, or ``None`` if not found."""
        url = self._url(f"/api/products/by-barcode/{barcode}")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to look up barcode {barcode}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to look up barcode {barcode}: {exc}"
            ) from exc

    def create_product(
        self,
        name: str,
        description: str = "",
        location_id: Optional[int] = None,
        unit_id: Optional[int] = None,
        **kwargs,
    ) -> int:
        """Create a new product and return its ID.

        Parameters
        ----------
        name:
            Product display name.
        description:
            Optional product description.
        location_id:
            Storage location ID.
        unit_id:
            Quantity unit ID.  **Required** by the Storage API – raises
            :class:`StorageAPIError` if ``None``.
        **kwargs:
            Extra fields forwarded to the API (e.g. ``product_group_id``).
        """
        if unit_id is None:
            raise StorageAPIError(
                f"Cannot create product '{name}': unit_id is required"
            )

        payload: dict = {
            "name": name,
            "description": description,
            "location_id": location_id,
            "unit_id": unit_id,
            "default_best_before_days": 60,
            **kwargs,
        }

        url = self._url("/api/products")
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return int(resp.json()["id"])
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to create product '{name}': {exc}{body}"
            ) from exc
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise StorageAPIError(
                f"Failed to create product '{name}': {exc}"
            ) from exc

    def update_product(self, product_id: int, **fields) -> None:
        """Partially update an existing product."""
        url = self._url(f"/api/products/{product_id}")
        try:
            resp = self._session.put(url, json=fields, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to update product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to update product {product_id}: {exc}"
            ) from exc

    def delete_product(self, product_id: int) -> None:
        """Delete a product by ID."""
        url = self._url(f"/api/products/{product_id}")
        try:
            resp = self._session.delete(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to delete product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to delete product {product_id}: {exc}"
            ) from exc

    def get_all_products(self, active_only: bool = False) -> list[dict]:
        """Return all products (or only active ones)."""
        url = self._url("/api/products")
        params = {"active_only": str(active_only).lower()}
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch products: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(f"Failed to fetch products: {exc}") from exc

    # ------------------------------------------------------------------
    # Barcodes
    # ------------------------------------------------------------------

    def add_barcode(
        self,
        product_id: int,
        barcode: str,
        pack_size: float = 1.0,
        pack_unit_id: Optional[int] = None,
    ) -> None:
        """Associate *barcode* with *product_id*."""
        payload: dict = {
            "product_id": product_id,
            "barcode": barcode,
            "pack_size": pack_size,
            "pack_unit_id": pack_unit_id,
        }
        url = self._url("/api/barcodes")
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to add barcode {barcode} to product {product_id}: "
                f"{exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to add barcode {barcode} to product {product_id}: "
                f"{exc}"
            ) from exc

    def get_all_barcodes(self) -> list[dict]:
        """Return all barcode entries."""
        url = self._url("/api/barcodes")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch barcodes: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(f"Failed to fetch barcodes: {exc}") from exc

    def get_product_barcodes(self, product_id: int) -> list[dict]:
        """Return barcode entries for *product_id* (client-side filter)."""
        all_barcodes = self.get_all_barcodes()
        return [
            b for b in all_barcodes
            if int(b.get("product_id", 0)) == product_id
        ]

    def update_barcode(self, barcode_id: int, **fields) -> None:
        """Update fields on an existing barcode entry."""
        url = self._url(f"/api/barcodes/{barcode_id}")
        try:
            resp = self._session.put(url, json=fields, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to update barcode {barcode_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to update barcode {barcode_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Barcode Queue
    # ------------------------------------------------------------------

    def get_barcode_queue(self, status: str = "pending") -> list[dict]:
        """Return barcode-queue items filtered by *status*."""
        url = self._url(f"/api/barcode-queue?status={status}")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to fetch barcode queue{_response_error_detail(getattr(exc, 'response', None))}"
            ) from exc

    def add_to_barcode_queue(self, barcode: str, source: str = "scraper") -> dict:
        """Add a barcode to the queue. Returns the created item."""
        url = self._url("/api/barcode-queue")
        try:
            resp = self._session.post(
                url,
                json={"barcode": barcode, "source": source},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to add barcode to queue{_response_error_detail(getattr(exc, 'response', None))}"
            ) from exc

    def update_barcode_queue_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        result_product_id: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update fields on a barcode-queue item."""
        url = self._url(f"/api/barcode-queue/{item_id}")
        payload: dict = {}
        if status is not None:
            payload["status"] = status
        if result_product_id is not None:
            payload["result_product_id"] = result_product_id
        if error_message is not None:
            payload["error_message"] = error_message
        try:
            resp = self._session.put(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to update barcode queue item {item_id}"
                f"{_response_error_detail(getattr(exc, 'response', None))}"
            ) from exc

    def delete_barcode_queue_item(self, item_id: int) -> None:
        """Delete a barcode-queue item by ID."""
        url = self._url(f"/api/barcode-queue/{item_id}")
        try:
            resp = self._session.delete(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to delete barcode queue item {item_id}"
                f"{_response_error_detail(getattr(exc, 'response', None))}"
            ) from exc

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    def upload_product_image(
        self,
        product_id: int,
        filename: str,
        image_bytes: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload an image file and link it to *product_id*.

        Two-step process:
        1. ``PUT /api/files/products/{filename}`` with raw bytes.
        2. ``PUT /api/products/{product_id}`` with ``picture_filename``.
        """
        file_url = self._url(f"/api/files/products/{filename}")
        try:
            resp = self._session.put(
                file_url,
                data=image_bytes,
                headers={"Content-Type": content_type},
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to upload image for product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to upload image for product {product_id}: {exc}"
            ) from exc

        # Link the uploaded file to the product.
        prod_url = self._url(f"/api/products/{product_id}")
        try:
            resp = self._session.put(
                prod_url,
                json={"picture_filename": filename},
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to set picture for product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to set picture for product {product_id}: {exc}"
            ) from exc

    def delete_product_image(self, filename: str) -> None:
        """Delete a product picture file.  Silently ignores 404."""
        url = self._url(f"/api/files/products/{filename}")
        try:
            resp = self._session.delete(url, timeout=self.timeout)
            if resp.status_code == 404:
                return
            resp.raise_for_status()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to delete image '{filename}': {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to delete image '{filename}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------

    def add_stock(
        self,
        product_id: int,
        amount: float = 1.0,
        location_id: Optional[int] = None,
    ) -> None:
        """Add *amount* units of *product_id* to stock."""
        payload: dict = {"product_id": product_id, "amount": amount}
        if location_id is not None:
            payload["location_id"] = location_id

        url = self._url("/api/stock/add")
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to add stock for product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to add stock for product {product_id}: {exc}"
            ) from exc

    def get_product_stock_locations(self, product_id: int) -> list[dict]:
        """Return stock entries for *product_id* (each has ``location_id``, ``amount``)."""
        url = self._url(f"/api/stock/product/{product_id}")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch stock locations for product {product_id}: "
                f"{exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to fetch stock locations for product {product_id}: "
                f"{exc}"
            ) from exc

    def transfer_stock(
        self,
        product_id: int,
        amount: float,
        location_id_from: int,
        location_id_to: int,
    ) -> None:
        """Transfer *amount* units between locations."""
        url = self._url("/api/stock/transfer")
        payload = {
            "product_id": product_id,
            "amount": amount,
            "from_location_id": location_id_from,
            "to_location_id": location_id_to,
        }
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to transfer stock for product {product_id}: "
                f"{exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to transfer stock for product {product_id}: {exc}"
            ) from exc

    def get_stock_entries(self, product_id: int | None = None) -> list[dict]:
        """Return stock entries, optionally filtered by product.

        * With *product_id*: ``GET /api/stock/product/{id}``
        * Without: ``GET /api/stock`` (returns summary list)
        """
        if product_id is not None:
            url = self._url(f"/api/stock/product/{product_id}")
        else:
            url = self._url("/api/stock")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch stock entries: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to fetch stock entries: {exc}"
            ) from exc

    def delete_stock_entry(self, entry_id: int) -> None:
        """Delete a single stock entry by ID."""
        url = self._url(f"/api/stock/{entry_id}")
        try:
            resp = self._session.delete(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to delete stock entry {entry_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to delete stock entry {entry_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------

    def get_locations(self) -> list[dict]:
        """Return all storage locations."""
        url = self._url("/api/locations")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch locations: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(f"Failed to fetch locations: {exc}") from exc

    # ------------------------------------------------------------------
    # Product groups
    # ------------------------------------------------------------------

    def ensure_product_group(self, name: str) -> int:
        """Return the ID of the product group *name*, creating it if needed."""
        url = self._url("/api/product-groups")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            groups = resp.json() or []
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to fetch product groups: {exc}"
            ) from exc

        for group in groups:
            if group.get("name") == name:
                return int(group["id"])

        # Group does not exist – create it.
        try:
            resp = self._session.post(url, json={"name": name}, timeout=self.timeout)
            resp.raise_for_status()
            return int(resp.json()["id"])
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to create product group '{name}': {exc}{body}"
            ) from exc
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise StorageAPIError(
                f"Failed to create product group '{name}': {exc}"
            ) from exc

    def get_product_groups(self) -> list[dict]:
        """Return all product groups."""
        url = self._url("/api/product-groups")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch product groups: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to fetch product groups: {exc}"
            ) from exc

    def delete_product_group(self, group_id: int) -> None:
        """Delete a product group by ID."""
        url = self._url(f"/api/product-groups/{group_id}")
        try:
            resp = self._session.delete(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to delete product group {group_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to delete product group {group_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Quantity units & conversions
    # ------------------------------------------------------------------

    def get_quantity_units(self) -> list[dict]:
        """Return all quantity units.

        Adds a ``description`` alias mapped from ``abbreviation`` for
        backward compatibility with code that reads the Grocy field name.
        """
        url = self._url("/api/units")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            units = resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch quantity units: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to fetch quantity units: {exc}"
            ) from exc

        for unit in units:
            unit.setdefault("description", unit.get("abbreviation", ""))
        return units

    def create_quantity_unit(
        self,
        name: str,
        abbreviation: str = "",
        name_plural: str = "",
    ) -> int:
        """Create a quantity unit and return its ID.

        Handles HTTP 409 (already exists) by looking up and returning
        the existing unit's ID.
        """
        url = self._url("/api/units")
        payload: dict = {
            "name": name,
            "abbreviation": abbreviation,
            "name_plural": name_plural,
        }
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return int(resp.json()["id"])
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                return self._find_quantity_unit_by_name(name)
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to create QU '{name}': {exc}{body}"
            ) from exc
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise StorageAPIError(
                f"Failed to create QU '{name}': {exc}"
            ) from exc

    def get_quantity_unit_conversions(self) -> list[dict]:
        """Return all quantity unit conversions.

        Adds ``from_qu_id`` / ``to_qu_id`` aliases for backward
        compatibility with code that reads the Grocy field names.
        """
        url = self._url("/api/conversions")
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            conversions = resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch QU conversions: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to fetch QU conversions: {exc}"
            ) from exc

        for conv in conversions:
            conv.setdefault("from_qu_id", conv.get("from_unit_id"))
            conv.setdefault("to_qu_id", conv.get("to_unit_id"))
        return conversions

    def create_quantity_unit_conversion(
        self,
        from_unit_id: int,
        to_unit_id: int,
        factor: float,
        product_id: int | None = None,
    ) -> int:
        """Create a quantity unit conversion and return its ID.

        Handles HTTP 409 (already exists) gracefully by returning 0.
        """
        url = self._url("/api/conversions")
        payload: dict = {
            "from_unit_id": from_unit_id,
            "to_unit_id": to_unit_id,
            "factor": factor,
        }
        if product_id is not None:
            payload["product_id"] = product_id
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return int(resp.json()["id"])
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                logger.debug(
                    "QU conversion %s→%s already exists (409), skipping",
                    from_unit_id, to_unit_id,
                )
                return 0
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to create QU conversion: {exc}{body}"
            ) from exc
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise StorageAPIError(
                f"Failed to create QU conversion: {exc}"
            ) from exc

    def delete_quantity_unit_conversion(self, conversion_id: int) -> None:
        """Delete a quantity unit conversion by ID."""
        url = self._url(f"/api/conversions/{conversion_id}")
        try:
            resp = self._session.delete(url, timeout=self.timeout)
            if resp.status_code == 404:
                return
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to delete QU conversion {conversion_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to delete QU conversion {conversion_id}: {exc}"
            ) from exc

    def delete_quantity_unit(self, qu_id: int) -> None:
        """Delete a quantity unit by ID."""
        url = self._url(f"/api/units/{qu_id}")
        try:
            resp = self._session.delete(url, timeout=self.timeout)
            if resp.status_code == 404:
                return
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to delete QU {qu_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to delete QU {qu_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Recipes
    # ------------------------------------------------------------------

    def get_recipe_positions(self) -> list[dict]:
        """Return all recipe ingredient positions across every recipe.

        Fetches every recipe, then every recipe's ingredients, and
        flattens them into a single list.  Each dict gets a ``recipe_id``
        field and a ``qu_id`` alias for ``unit_id``.  An internal mapping
        is cached so :meth:`update_recipe_position` can route updates.
        """
        recipes_url = self._url("/api/recipes")
        try:
            resp = self._session.get(recipes_url, timeout=self.timeout)
            resp.raise_for_status()
            recipes = resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to fetch recipes: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(f"Failed to fetch recipes: {exc}") from exc

        positions: list[dict] = []
        self._ingredient_to_recipe.clear()

        for recipe in recipes:
            recipe_id = int(recipe["id"])
            detail_url = self._url(f"/api/recipes/{recipe_id}")
            try:
                resp = self._session.get(detail_url, timeout=self.timeout)
                resp.raise_for_status()
                detail = resp.json()
            except requests.HTTPError as exc:
                body = _response_error_detail(exc.response)
                logger.warning(
                    "Skipping recipe %s: %s%s", recipe_id, exc, body,
                )
                continue
            except requests.RequestException as exc:
                logger.warning("Skipping recipe %s: %s", recipe_id, exc)
                continue

            ingredients = detail.get("ingredients") or []
            for ing in ingredients:
                ing["recipe_id"] = recipe_id
                ing.setdefault("qu_id", ing.get("unit_id"))
                ingredient_id = int(ing["id"])
                self._ingredient_to_recipe[ingredient_id] = recipe_id
                positions.append(ing)

        return positions

    def update_recipe_position(self, pos_id: int, **fields) -> None:
        """Update fields on a recipe ingredient.

        Uses the internal ``_ingredient_to_recipe`` mapping (populated by
        :meth:`get_recipe_positions`) to determine the parent recipe.

        Parameters
        ----------
        pos_id:
            Ingredient ID (as returned in ``get_recipe_positions``).
        **fields:
            Fields to update (e.g. ``qu_id=7``).
        """
        recipe_id = self._ingredient_to_recipe.get(pos_id)
        if recipe_id is None:
            raise StorageAPIError(
                f"Unknown recipe for ingredient {pos_id} – call "
                f"get_recipe_positions() first to populate the mapping"
            )

        url = self._url(f"/api/recipes/{recipe_id}/ingredients/{pos_id}")
        try:
            resp = self._session.put(url, json=fields, timeout=self.timeout)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise StorageAPIError(
                f"Failed to update recipe position {pos_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise StorageAPIError(
                f"Failed to update recipe position {pos_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build an absolute URL from a relative API path."""
        return self.base_url + "/" + path.lstrip("/")

    def _find_quantity_unit_by_name(self, name: str) -> int:
        """Look up an existing quantity unit by *name* and return its ID.

        Raises :class:`StorageAPIError` if the unit cannot be found.
        """
        units = self.get_quantity_units()
        for unit in units:
            if unit.get("name") == name:
                return int(unit["id"])
        raise StorageAPIError(
            f"QU '{name}' reported as duplicate (409) but not found in listing"
        )
