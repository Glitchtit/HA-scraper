"""Grocy REST API client.

Provides the minimal surface needed to:

* Look up whether a product with a given barcode already exists.
* Create a new product.
* Add (or update) a barcode entry for a product.
* Upload a product picture.

Grocy API reference: https://demo.grocy.info/api/
Authentication is done via the ``GROCY-API-KEY`` HTTP header.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


def _response_error_detail(response: requests.Response | None) -> str:
    """Extract a human-readable error detail from a Grocy error response."""
    if response is None:
        return ""
    try:
        data = response.json()
        detail = data.get("error_message") or data.get("error_detail") or ""
        if detail:
            return f" – {detail}"
    except (ValueError, AttributeError):
        pass
    text = response.text.strip()
    if text:
        return f" – {text[:300]}"
    return ""


class GrocyAPIError(Exception):
    """Raised when the Grocy API returns an unexpected response."""


class GrocyClient:
    """Thin HTTP client for a self-hosted Grocy instance.

    Parameters
    ----------
    base_url:
        The root URL of the Grocy instance, e.g. ``"https://grocy.example.com"``.
    api_key:
        A valid Grocy API key (created in Grocy's user settings).
    session:
        An optional :class:`requests.Session` to reuse.  Useful for testing.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "GROCY-API-KEY": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_product_by_barcode(self, barcode: str) -> Optional[dict]:
        """Return the Grocy product dict for *barcode*, or ``None`` if not found.

        Uses the stock endpoint which resolves the barcode to a product.
        """
        url = self._url(f"/api/stock/products/by-barcode/{barcode}")
        try:
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 400:
                # Grocy returns 400 when the barcode is not registered.
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("product") or data
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                return None
            raise GrocyAPIError(
                f"HTTP error {exc.response.status_code} looking up barcode {barcode}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(f"Request failed: {exc}") from exc

    def create_product(
        self,
        name: str,
        description: str = "",
        location_id: Optional[int] = None,
        quantity_unit_id: Optional[int] = None,
    ) -> int:
        """Create a new product and return its Grocy product ID.

        Parameters
        ----------
        name:
            Product display name.
        description:
            Optional product description.
        location_id:
            Grocy location ID to assign.  If ``None`` the default location
            configured in Grocy is used.
        quantity_unit_id:
            Grocy quantity unit ID.  If ``None`` the default unit is used.
        """
        payload: dict = {
            "name": name,
            "treat_opened_as_out_of_stock": 0,
            "default_best_before_days": 60,
        }
        if description:
            payload["description"] = description
        if location_id is not None:
            payload["location_id"] = location_id
        if quantity_unit_id is not None:
            payload["qu_id_stock"] = quantity_unit_id
            payload["qu_id_purchase"] = quantity_unit_id

        url = self._url("/api/objects/products")
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return int(resp.json()["created_object_id"])
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to create product '{name}': {exc}{body}"
            ) from exc
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise GrocyAPIError(f"Failed to create product '{name}': {exc}") from exc

    def add_barcode(
        self,
        product_id: int,
        barcode: str,
        amount: float = 1.0,
        quantity_unit_id: Optional[int] = None,
    ) -> None:
        """Associate *barcode* with *product_id* in Grocy.

        Parameters
        ----------
        product_id:
            The Grocy internal product ID.
        barcode:
            EAN / barcode string.
        amount:
            Amount per scan (default 1.0).
        quantity_unit_id:
            Optional quantity unit override for this barcode.
        """
        payload: dict = {
            "product_id": product_id,
            "barcode": barcode,
            "amount": amount,
        }
        if quantity_unit_id is not None:
            payload["qu_id"] = quantity_unit_id

        url = self._url("/api/objects/product_barcodes")
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to add barcode {barcode} to product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to add barcode {barcode} to product {product_id}: {exc}"
            ) from exc

    def upload_product_image(
        self,
        product_id: int,
        filename: str,
        image_bytes: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload an image for *product_id* and set it as the product picture.

        Parameters
        ----------
        product_id:
            The Grocy internal product ID.
        filename:
            Filename to store in Grocy (e.g. ``"6410405082657.jpg"``).
        image_bytes:
            Raw image file contents.
        content_type:
            MIME type of the image (e.g. ``"image/jpeg"``).
        """
        encoded_name = base64.b64encode(filename.encode()).decode()
        url = self._url(f"/api/files/productpictures/{encoded_name}")
        try:
            resp = self._session.put(
                url,
                data=image_bytes,
                headers={"Content-Type": content_type},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                # File may already exist — delete it and retry once.
                try:
                    self._session.delete(url, timeout=10).raise_for_status()
                    resp = self._session.put(
                        url,
                        data=image_bytes,
                        headers={"Content-Type": content_type},
                        timeout=30,
                    )
                    resp.raise_for_status()
                except requests.RequestException as retry_exc:
                    body = _response_error_detail(
                        getattr(retry_exc, "response", None)
                    )
                    raise GrocyAPIError(
                        f"Failed to upload image for product {product_id} "
                        f"(after retry): {retry_exc}{body}"
                    ) from retry_exc
            else:
                body = _response_error_detail(exc.response)
                raise GrocyAPIError(
                    f"Failed to upload image for product {product_id}: {exc}{body}"
                ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to upload image for product {product_id}: {exc}"
            ) from exc

        # Link the uploaded file to the product.
        obj_url = self._url(f"/api/objects/products/{product_id}")
        try:
            resp = self._session.put(
                obj_url, json={"picture_file_name": filename}, timeout=10
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to set picture for product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to set picture for product {product_id}: {exc}"
            ) from exc

    def get_all_products(self) -> list[dict]:
        """Return a list of all products in the Grocy database."""
        url = self._url("/api/objects/products")
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except requests.RequestException as exc:
            raise GrocyAPIError(f"Failed to fetch products: {exc}") from exc

    def get_locations(self) -> list[dict]:
        """Return a list of all locations in the Grocy database."""
        url = self._url("/api/objects/locations")
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except requests.RequestException as exc:
            raise GrocyAPIError(f"Failed to fetch locations: {exc}") from exc

    def ensure_product_group(self, name: str) -> int:
        """Return the ID of the product group *name*, creating it if needed.

        Queries ``/api/objects/product_groups`` for an existing group with the
        given name.  If none is found a new group is created and its ID is
        returned.

        Parameters
        ----------
        name:
            The product group name (e.g. ``"Group master"``).
        """
        url = self._url("/api/objects/product_groups")
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            groups = resp.json() or []
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to fetch product groups: {exc}"
            ) from exc

        for group in groups:
            if group.get("name") == name:
                return int(group["id"])

        # Group does not exist yet – create it.
        try:
            resp = self._session.post(url, json={"name": name}, timeout=10)
            resp.raise_for_status()
            return int(resp.json()["created_object_id"])
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to create product group '{name}': {exc}{body}"
            ) from exc
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise GrocyAPIError(
                f"Failed to create product group '{name}': {exc}"
            ) from exc

    def update_product(self, product_id: int, **fields) -> None:
        """Update fields on an existing product.

        Parameters
        ----------
        product_id:
            The Grocy internal product ID.
        **fields:
            Arbitrary product fields to update (e.g. ``location_id=2``,
            ``default_best_before_days=14``).
        """
        url = self._url(f"/api/objects/products/{product_id}")
        try:
            resp = self._session.put(url, json=fields, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to update product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(f"Failed to update product {product_id}: {exc}") from exc

    def add_stock(self, product_id: int, amount: float = 1.0) -> None:
        """Add *amount* units of *product_id* to Grocy stock.

        Uses the ``/api/stock/products/{id}/add`` endpoint.

        Parameters
        ----------
        product_id:
            The Grocy internal product ID.
        amount:
            Number of units to add (default 1.0).
        """
        url = self._url(f"/api/stock/products/{product_id}/add")
        payload = {"amount": amount, "transaction_type": "purchase"}
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to add stock for product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to add stock for product {product_id}: {exc}"
            ) from exc

    def get_all_barcodes(self) -> list[dict]:
        """Return a list of all product barcode entries in Grocy."""
        url = self._url("/api/objects/product_barcodes")
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except requests.RequestException as exc:
            raise GrocyAPIError(f"Failed to fetch barcodes: {exc}") from exc

    def delete_product(self, product_id: int) -> None:
        """Delete a product from Grocy by its internal ID.

        Parameters
        ----------
        product_id:
            The Grocy internal product ID.
        """
        url = self._url(f"/api/objects/products/{product_id}")
        try:
            resp = self._session.delete(url, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to delete product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to delete product {product_id}: {exc}"
            ) from exc

    def get_product_stock_locations(self, product_id: int) -> list[dict]:
        """Return stock amounts per location for *product_id*.

        Uses the ``/api/stock/products/{id}/locations`` endpoint.
        Each entry contains at least ``location_id`` and ``amount``.
        Returns an empty list if the product has no stock.
        """
        url = self._url(f"/api/stock/products/{product_id}/locations")
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to fetch stock locations for product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to fetch stock locations for product {product_id}: {exc}"
            ) from exc

    def transfer_stock(
        self,
        product_id: int,
        amount: float,
        location_id_from: int,
        location_id_to: int,
    ) -> None:
        """Transfer *amount* units of *product_id* between locations.

        Uses the ``/api/stock/products/{id}/transfer`` endpoint.

        Parameters
        ----------
        product_id:
            The Grocy internal product ID.
        amount:
            Number of units to transfer.
        location_id_from:
            Source location ID.
        location_id_to:
            Destination location ID.
        """
        url = self._url(f"/api/stock/products/{product_id}/transfer")
        payload = {
            "amount": amount,
            "location_id_from": location_id_from,
            "location_id_to": location_id_to,
        }
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to transfer stock for product {product_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to transfer stock for product {product_id}: {exc}"
            ) from exc

    def get_product_barcodes(self, product_id: int) -> list[dict]:
        """Return barcode entries for *product_id*.

        Fetches all barcodes and filters by ``product_id``.  Each entry
        contains at least ``id``, ``product_id``, ``barcode``, and
        ``amount``.
        """
        all_barcodes = self.get_all_barcodes()
        return [b for b in all_barcodes if int(b.get("product_id", 0)) == product_id]

    def update_barcode(self, barcode_id: int, **fields) -> None:
        """Update fields on an existing barcode entry.

        Parameters
        ----------
        barcode_id:
            The Grocy internal barcode entry ID (not the barcode string).
        **fields:
            Arbitrary barcode fields to update (e.g. ``product_id=5``,
            ``amount=4``).
        """
        url = self._url(f"/api/objects/product_barcodes/{barcode_id}")
        try:
            resp = self._session.put(url, json=fields, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to update barcode {barcode_id}: {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to update barcode {barcode_id}: {exc}"
            ) from exc

    def delete_product_image(self, filename: str) -> None:
        """Delete a product picture file from Grocy.

        Parameters
        ----------
        filename:
            The picture filename (e.g. ``"6410405082657.png"``).
            Ignored silently if the file does not exist (404).
        """
        encoded_name = base64.b64encode(filename.encode()).decode()
        url = self._url(f"/api/files/productpictures/{encoded_name}")
        try:
            resp = self._session.delete(url, timeout=10)
            if resp.status_code == 404:
                return  # File doesn't exist — nothing to do.
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = _response_error_detail(exc.response)
            raise GrocyAPIError(
                f"Failed to delete image '{filename}': {exc}{body}"
            ) from exc
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to delete image '{filename}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))
