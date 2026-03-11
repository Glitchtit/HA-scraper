"""Grocy REST API client.

Provides the minimal surface needed to:

* Look up whether a product with a given barcode already exists.
* Create a new product.
* Add (or update) a barcode entry for a product.

Grocy API reference: https://demo.grocy.info/api/
Authentication is done via the ``GROCY-API-KEY`` HTTP header.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


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
        payload: dict = {"name": name}
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
        except requests.RequestException as exc:
            raise GrocyAPIError(
                f"Failed to add barcode {barcode} to product {product_id}: {exc}"
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

    def get_all_barcodes(self) -> list[dict]:
        """Return a list of all product barcode entries in Grocy."""
        url = self._url("/api/objects/product_barcodes")
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except requests.RequestException as exc:
            raise GrocyAPIError(f"Failed to fetch barcodes: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))
