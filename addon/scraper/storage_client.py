"""HA-Storage REST API client.

Talks to the custom HA-Storage addon (FastAPI) running inside the trusted
HA network.

Key characteristics
-------------------
* No API key required — Storage runs inside the trusted HA network.
* Created objects return ``{"id": …}``.
* File uploads use plain filenames (no Base64 encoding).
* Error responses follow the FastAPI ``{"detail": "…"}`` convention.
"""

from __future__ import annotations

import logging
import posixpath
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

        # Cached default unit ID (lazy-loaded on first use).
        self._default_unit_id: int | None = None

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def get_default_unit_id(self) -> int:
        """Return the ID of the default quantity unit ("kpl" / piece).

        Lazily fetches the unit list from Storage on the first call and
        caches the result for the session.  Falls back to the first
        available unit if "kpl" is not found.
        """
        if self._default_unit_id is not None:
            return self._default_unit_id

        units = self.get_quantity_units()
        for u in units:
            if u.get("abbreviation", "").lower() == "kpl":
                self._default_unit_id = int(u["id"])
                return self._default_unit_id
        # Fallback: first unit in the database.
        if units:
            self._default_unit_id = int(units[0]["id"])
            return self._default_unit_id
        raise StorageAPIError("No quantity units found in Storage")

    def get_product_by_barcode(self, barcode: str) -> Optional[dict]:
        """Return the product dict for *barcode*, or ``None`` if not found."""
        resp = self._request(
            "get", f"/api/products/by-barcode/{barcode}",
            f"look up barcode {barcode}", missing_ok=True,
        )
        return resp.json() if resp is not None else None

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
            Quantity unit ID.  If ``None``, auto-detects the default unit
            ("kpl") from Storage.
        **kwargs:
            Extra fields forwarded to the API (e.g. ``product_group_id``).
        """
        if unit_id is None:
            unit_id = self.get_default_unit_id()
            logger.debug("Auto-detected default unit_id=%d for '%s'.", unit_id, name)

        payload: dict = {
            "name": name,
            "description": description,
            "location_id": location_id,
            "unit_id": unit_id,
            "default_best_before_days": 60,
            **kwargs,
        }
        resp = self._request(
            "post", "/api/products", f"create product '{name}'", json=payload
        )
        try:
            return int(resp.json()["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageAPIError(
                f"Failed to create product '{name}': {exc}"
            ) from exc

    def update_product(self, product_id: int, **fields) -> None:
        """Partially update an existing product."""
        self._request(
            "put", f"/api/products/{product_id}",
            f"update product {product_id}", json=fields,
        )

    def delete_product(self, product_id: int) -> None:
        """Delete a product by ID."""
        self._request(
            "delete", f"/api/products/{product_id}", f"delete product {product_id}"
        )

    def get_all_products(self, active_only: bool = False) -> list[dict]:
        """Return all products (or only active ones)."""
        resp = self._request(
            "get", "/api/products", "fetch products",
            params={"active_only": str(active_only).lower()},
        )
        return resp.json() or []

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
        self._request(
            "post", "/api/barcodes",
            f"add barcode {barcode} to product {product_id}", json=payload,
        )

    def get_all_barcodes(self) -> list[dict]:
        """Return all barcode entries."""
        return self._request("get", "/api/barcodes", "fetch barcodes").json() or []

    # ------------------------------------------------------------------
    # Stores / per-store availability
    # ------------------------------------------------------------------

    def upsert_store(self, store_id: str, name: str) -> None:
        """Register or rename a store (K-group store ID → display name)."""
        self._request(
            "put", f"/api/stores/{store_id}", f"upsert store {store_id}",
            json={"name": name},
        )

    def set_product_availability(
        self, product_id: int, entries: list[dict]
    ) -> None:
        """Upsert per-store availability rows for *product_id*.

        Each entry is ``{"store_id", "available"}`` plus optional ``"price"``
        and ``"price_currency"``.  Stores absent from *entries* keep their
        previous state in Storage.
        """
        self._request(
            "put", f"/api/products/{product_id}/availability",
            f"set availability for product {product_id}", json=entries,
        )

    # ------------------------------------------------------------------
    # Barcode Queue
    # ------------------------------------------------------------------

    def get_barcode_queue(self, status: str = "pending") -> list[dict]:
        """Return barcode-queue items filtered by *status*."""
        return self._request(
            "get", f"/api/barcode-queue?status={status}", "fetch barcode queue"
        ).json()

    def update_barcode_queue_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        result_product_id: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update fields on a barcode-queue item."""
        payload: dict = {}
        if status is not None:
            payload["status"] = status
        if result_product_id is not None:
            payload["result_product_id"] = result_product_id
        if error_message is not None:
            payload["error_message"] = error_message
        self._request(
            "put", f"/api/barcode-queue/{item_id}",
            f"update barcode queue item {item_id}", json=payload,
        )

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
        self._request(
            "put", f"/api/files/products/{filename}",
            f"upload image for product {product_id}",
            data=image_bytes, headers={"Content-Type": content_type},
        )
        self._request(
            "put", f"/api/products/{product_id}",
            f"set picture for product {product_id}",
            json={"picture_filename": filename},
        )

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
        self._request(
            "post", "/api/stock/add", f"add stock for product {product_id}",
            json=payload,
        )

    # ------------------------------------------------------------------
    # Quantity units
    # ------------------------------------------------------------------

    def get_quantity_units(self) -> list[dict]:
        """Return all quantity units."""
        return self._request("get", "/api/units", "fetch quantity units").json() or []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build an absolute URL from a relative API path."""
        return self.base_url + "/" + path.lstrip("/")

    def _request(
        self, method: str, path: str, action: str, *,
        missing_ok: bool = False, **kwargs,
    ) -> Optional[requests.Response]:
        """Send one request and return the response.

        Every failure is raised as ``StorageAPIError("Failed to <action>: …")``,
        with Storage's own error detail when it sent one.  With *missing_ok*
        an HTTP 404 returns ``None`` instead.
        """
        try:
            resp = getattr(self._session, method)(
                self._url(path), timeout=self.timeout, **kwargs
            )
            if missing_ok and resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            detail = _response_error_detail(getattr(exc, "response", None))
            raise StorageAPIError(f"Failed to {action}: {exc}{detail}") from exc


# ------------------------------------------------------------------
# Creating a product
# ------------------------------------------------------------------

def create_in_storage(
    storage: StorageClient,
    found,
    *,
    ean: str,
    price: Optional[float] = None,
    parent_id: Optional[int] = None,
    product_group_id: Optional[int] = None,
    availability: Optional[list[dict]] = None,
) -> Optional[int]:
    """Create the product *found* in Storage and return its ID.

    *found* carries ``name``, ``description`` and ``image_url`` (a
    ``Product``).  Returns ``None``, writing nothing, when *ean* already
    belongs to a product.  The sequence every caller shares:

    1. Create the product, with *price* as its ``unit_price`` and with
       *parent_id* / *product_group_id* when given.
    2. Attach *ean* as its barcode.  A product without its barcode is never
       found by a scan, so every later scan would create another one: when
       the barcode cannot be attached the product is deleted again and the
       error raised.
    3. Download and upload the image, and write the per-store
       *availability* rows.  Both are best-effort: a failure is logged.

    Raises :class:`StorageAPIError` when the product could not be created or
    its barcode attached.
    """
    if ean and storage.get_product_by_barcode(ean):
        return None

    fields: dict = {}
    if price is not None:
        fields["unit_price"] = price
        fields["unit_price_currency"] = "EUR"
    if parent_id is not None:
        fields["parent_id"] = parent_id
    if product_group_id is not None:
        fields["product_group_id"] = product_group_id
    product_id = storage.create_product(
        found.name, description=found.description, **fields
    )
    logger.info("Created product '%s' (Storage product ID %d).", found.name, product_id)

    if ean:
        try:
            storage.add_barcode(product_id, ean)
        except StorageAPIError as exc:
            logger.error(
                "Product '%s' created but failed to add barcode %s: %s",
                found.name, ean, exc,
            )
            try:
                storage.delete_product(product_id)
                logger.info("  → Deleted product %d again.", product_id)
            except StorageAPIError as del_exc:
                logger.error(
                    "Could not delete barcode-less product %d: %s", product_id, del_exc
                )
            raise
        logger.info("  → Added barcode %s.", ean)

    if found.image_url:
        upload_image(storage, product_id, found.image_url, ean or f"product-{product_id}")
    if availability:
        write_availability(storage, product_id, availability)
    return product_id


def write_availability(
    storage: StorageClient, product_id: int, entries: list[dict]
) -> None:
    """Upsert per-store availability *entries* for a product.

    Best-effort: Storage errors are logged, never raised. No-op if *entries*
    is empty.
    """
    if not entries:
        return
    try:
        storage.set_product_availability(product_id, entries)
        logger.info(
            "  → Availability: %s",
            ", ".join(f"{e['store_id']}={'✓' if e['available'] else '✗'}"
                      for e in entries),
        )
    except StorageAPIError as exc:
        logger.warning(
            "Failed to write availability for product %d: %s", product_id, exc
        )


_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _image_extension(content_type: str, url: str) -> str:
    """Derive a file extension from the MIME type, falling back to the URL."""
    ext = _MIME_TO_EXT.get(content_type, "")
    if not ext:
        ext = posixpath.splitext(url.split("?")[0])[1] or ".jpg"
    return ext


def upload_image(
    storage: StorageClient, product_id: int, image_url: str, stem: str
) -> None:
    """Download *image_url* and make it the product's picture, ``<stem><ext>``.

    Best-effort: a failed download or upload is logged, never raised.
    """
    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Could not download image %s: %s", image_url, exc)
        return

    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    filename = f"{stem}{_image_extension(content_type, image_url)}"
    try:
        storage.upload_product_image(
            product_id, filename, resp.content, content_type=content_type
        )
        logger.info("  → Uploaded image %s.", filename)
    except StorageAPIError as exc:
        logger.warning("Could not upload image for product %d: %s", product_id, exc)
