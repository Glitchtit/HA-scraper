"""Scraper for https://www.k-ruoka.fi/kauppa.

Fetches product listings from the k-ruoka.fi internal JSON API and yields
:class:`Product` objects that contain the product name and EAN barcode.

The site is a single-page React application that calls an internal REST API
(`/kr-api/…`).  The endpoints used here were discovered by inspecting the
browser network traffic:

* ``GET /kr-api/products``  – paginated product catalogue
* ``GET /kr-api/search``    – keyword search across the catalogue
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.k-ruoka.fi"
_PRODUCTS_PATH = "/kr-api/products"
_SEARCH_PATH = "/kr-api/search"

# How many products to request per API page (the site uses 24 by default).
_PAGE_SIZE = 24

# Courtesy delay between requests (seconds).
_REQUEST_DELAY = 0.5

# HTTP headers that mimic a regular browser request so the server does not
# reject the scraper immediately.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    "Referer": "https://www.k-ruoka.fi/kauppa",
}


@dataclass
class Product:
    """A single product entry scraped from k-ruoka.fi."""

    name: str
    ean: str
    product_id: str = ""
    description: str = ""
    image_url: str = ""
    extra: dict = field(default_factory=dict)


class KRuokaScraper:
    """Scraper for the k-ruoka.fi product catalogue.

    Parameters
    ----------
    store_id:
        The K-group store identifier (e.g. ``"P048"``).  The store ID can be
        found in the URL when browsing the k-ruoka.fi shop after selecting a
        store, e.g. ``https://www.k-ruoka.fi/kauppa?storeId=P048``.
    session:
        An optional :class:`requests.Session` to reuse.  Useful for testing.
    request_delay:
        Seconds to wait between consecutive HTTP requests (default 0.5 s).
    """

    def __init__(
        self,
        store_id: str,
        session: Optional[requests.Session] = None,
        request_delay: float = _REQUEST_DELAY,
    ) -> None:
        self.store_id = store_id
        self.request_delay = request_delay
        self._session = session or requests.Session()
        self._session.headers.update(_HEADERS)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def search(self, query: str, max_products: Optional[int] = None) -> Iterator[Product]:
        """Yield products whose name or description matches *query*.

        Parameters
        ----------
        query:
            Free-text search term (Finnish or English).
        max_products:
            Stop after this many products.  ``None`` means no limit.
        """
        yield from self._paginate(_SEARCH_PATH, {"query": query}, max_products)

    def browse(self, max_products: Optional[int] = None) -> Iterator[Product]:
        """Yield all available products in the store catalogue.

        Parameters
        ----------
        max_products:
            Stop after this many products.  ``None`` means no limit.
        """
        yield from self._paginate(_PRODUCTS_PATH, {}, max_products)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _paginate(
        self,
        path: str,
        extra_params: dict,
        max_products: Optional[int],
    ) -> Iterator[Product]:
        """Handle pagination for a given API *path*."""
        offset = 0
        total_yielded = 0

        while True:
            params: dict = {
                "store": self.store_id,
                "limit": _PAGE_SIZE,
                "offset": offset,
                **extra_params,
            }
            url = urljoin(_BASE_URL, path)
            data = self._get_json(url, params)
            if data is None:
                break

            items = self._extract_items(data)
            if not items:
                logger.debug("No more items at offset=%d; stopping pagination.", offset)
                break

            for item in items:
                product = self._parse_product(item)
                if product is None:
                    continue
                yield product
                total_yielded += 1
                if max_products is not None and total_yielded >= max_products:
                    return

            # Check whether there are more pages.
            total = self._extract_total(data)
            offset += _PAGE_SIZE
            if total is not None and offset >= total:
                break

            time.sleep(self.request_delay)

    def _get_json(self, url: str, params: dict) -> Optional[dict]:
        """Perform a GET request and return parsed JSON, or ``None`` on failure."""
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.error("HTTP error %s for %s: %s", exc.response.status_code, url, exc)
        except requests.RequestException as exc:
            logger.error("Request failed for %s: %s", url, exc)
        except ValueError as exc:
            logger.error("Failed to decode JSON from %s: %s", url, exc)
        return None

    # ------------------------------------------------------------------
    # Data extraction helpers – these may need to be updated if the
    # k-ruoka.fi API response schema changes.
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_items(data: dict | list) -> list:
        """Return the list of raw product dicts from an API response."""
        # Some responses return a bare list at the top level.
        if isinstance(data, list):
            return data
        # The API may wrap items under different keys depending on the endpoint.
        for key in ("products", "items", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    @staticmethod
    def _extract_total(data: dict) -> Optional[int]:
        """Return the total number of available items, if present."""
        for key in ("total", "totalCount", "count"):
            if isinstance(data.get(key), int):
                return data[key]
        return None

    @staticmethod
    def _parse_product(item: dict) -> Optional[Product]:
        """Convert a raw API dict into a :class:`Product`.

        Returns ``None`` if the item is missing both a name and an EAN.
        """
        name: str = (
            item.get("name")
            or item.get("title")
            or item.get("productName")
            or ""
        ).strip()

        # EAN can appear under several keys.
        ean: str = (
            item.get("ean")
            or item.get("EAN")
            or item.get("barcode")
            or item.get("eanCode")
            or item.get("gtin")
            or ""
        ).strip()

        if not name and not ean:
            return None

        product_id: str = str(
            item.get("id")
            or item.get("productId")
            or item.get("sku")
            or ""
        )

        description: str = (
            item.get("description")
            or item.get("shortDescription")
            or ""
        ).strip()

        image_url: str = (
            item.get("imageUrl")
            or item.get("image")
            or item.get("thumbnail")
            or ""
        )

        return Product(
            name=name,
            ean=ean,
            product_id=product_id,
            description=description,
            image_url=image_url,
            extra=item,
        )
