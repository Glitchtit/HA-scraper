"""Scraper for https://www.k-ruoka.fi/kauppa.

Fetches product listings from the k-ruoka.fi internal REST API and yields
:class:`Product` objects that contain the product name and EAN barcode.

The site is a single-page React application protected by Cloudflare Bot
Management.  A Cloudflare clearance cookie (``cf_clearance``) is required
before the API endpoints respond with JSON.

**Cloudflare bypass** – tried in this order:

1. **FlareSolverr** (recommended): set ``FLARESOLVERR_URL`` in the environment
   or ``.env`` file.  FlareSolverr is a free Docker service that solves the
   challenge automatically::

       docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest

2. **Manual cookie injection**: set ``CF_CLEARANCE`` and ``CF_USER_AGENT``
   environment variables with values obtained from your browser's DevTools
   after visiting https://www.k-ruoka.fi/kauppa.

API endpoints used (all ``POST``, base ``https://www.k-ruoka.fi/kr-api``):

* ``/v2/product-search/{query}``  – paginated product search
* ``/offer-categories``           – list all offer category slugs
* ``/offer-category``             – paginated products in one category (max 25/page)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import urlencode, urljoin

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.k-ruoka.fi"
_KR_API = "/kr-api"

# Endpoint paths relative to _BASE_URL + _KR_API
_SEARCH_PATH = "v2/product-search/{query}"
_OFFER_CATEGORIES_PATH = "offer-categories"
_OFFER_CATEGORY_PATH = "offer-category"

# Items per page
_SEARCH_PAGE_SIZE = 100
_OFFER_CATEGORY_PAGE_SIZE = 25  # API returns 400 for values above 25

# Courtesy delay between requests (seconds) – proven safe rate.
_REQUEST_DELAY = 0.5

# HTTP headers required by the k-ruoka.fi kr-api.
# x-k-build-number and x-k-experiments are validated by the server;
# stale values cause 400 errors – update from DevTools if needed, or
# override with environment variables K_BUILD_NUMBER / K_EXPERIMENTS.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://www.k-ruoka.fi",
    "Referer": "https://www.k-ruoka.fi/kauppa",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
_DEFAULT_BUILD_NUMBER = "29159"
_DEFAULT_EXPERIMENTS = "ab4d.10001.0!d2ae.10003.0!a.00145.0!a.00150.0!a.00154.1"
# Default curl_cffi browser profile for TLS impersonation.
# Override with the CURL_CFFI_IMPERSONATE env var (e.g. "chrome131").
_DEFAULT_IMPERSONATE = "chrome124"


def _api_headers() -> dict:
    """Return the kr-api custom headers, allowing env-var overrides."""
    return {
        "x-k-build-number": os.environ.get("K_BUILD_NUMBER", _DEFAULT_BUILD_NUMBER),
        "x-k-experiments": os.environ.get("K_EXPERIMENTS", _DEFAULT_EXPERIMENTS),
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


# ---------------------------------------------------------------------------
# Cloudflare bypass helpers
# ---------------------------------------------------------------------------

def _resolve_cf_flaresolverr(site_url: str) -> tuple[dict, str]:
    """Obtain Cloudflare clearance cookies via a local FlareSolverr instance.

    Parameters
    ----------
    site_url:
        The URL to solve the challenge for (e.g. ``https://www.k-ruoka.fi/kauppa``).

    Returns
    -------
    (cookies, user_agent)
        The clearance cookies and the User-Agent that must be used with them.
    """
    flaresolverr_url = os.environ["FLARESOLVERR_URL"]
    logger.info("Resolving CF via FlareSolverr at %s …", flaresolverr_url)
    resp = requests.post(
        flaresolverr_url,
        json={"cmd": "request.get", "url": site_url, "maxTimeout": 90000},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(
            f"FlareSolverr returned status={data.get('status')!r}: {data}"
        )
    solution = data["solution"]
    cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
    user_agent = solution.get("userAgent", "")
    if "cf_clearance" not in cookies:
        raise RuntimeError(
            "FlareSolverr did not return cf_clearance cookie"
        )
    logger.info(
        "FlareSolverr: obtained %d cookies, UA=%s…", len(cookies), user_agent[:60]
    )
    return cookies, user_agent


def _build_session(cf_cookies: dict, user_agent: str) -> requests.Session:
    """Create a :class:`requests.Session` pre-loaded with CF clearance cookies."""
    impersonate = os.environ.get("CURL_CFFI_IMPERSONATE", _DEFAULT_IMPERSONATE)
    try:
        from curl_cffi.requests import Session as CurlSession  # type: ignore

        session = CurlSession(impersonate=impersonate)
    except ImportError:
        session = requests.Session()  # type: ignore[assignment]

    headers = {**_BROWSER_HEADERS, **_api_headers()}
    if user_agent:
        headers["User-Agent"] = user_agent
    session.headers.update(headers)

    for name, value in cf_cookies.items():
        session.cookies.set(name, value, domain=".k-ruoka.fi")  # type: ignore[union-attr]
    return session


def _make_session() -> requests.Session:
    """Build an authenticated session, trying all available CF bypass strategies.

    Strategy order:
    1. FlareSolverr (if ``FLARESOLVERR_URL`` env var is set).
    2. Pre-supplied cookies (if ``CF_CLEARANCE`` env var is set).
    3. curl_cffi TLS impersonation without cookies (may work on some CF tiers).
    4. Plain ``requests.Session`` (will likely get 403 – logs a clear warning).
    """
    site_url = f"{_BASE_URL}/kauppa"
    impersonate = os.environ.get("CURL_CFFI_IMPERSONATE", _DEFAULT_IMPERSONATE)

    # Strategy 1: FlareSolverr
    if os.environ.get("FLARESOLVERR_URL"):
        try:
            cookies, ua = _resolve_cf_flaresolverr(site_url)
            return _build_session(cookies, ua)
        except Exception as exc:
            logger.warning("FlareSolverr bypass failed: %s", exc)

    # Strategy 2: manually provided CF cookies
    cf_clearance = os.environ.get("CF_CLEARANCE")
    if cf_clearance:
        cf_ua = os.environ.get("CF_USER_AGENT", "")
        cookies = {"cf_clearance": cf_clearance}
        logger.info("Using CF_CLEARANCE cookie from environment.")
        return _build_session(cookies, cf_ua)

    # Strategy 3: curl_cffi TLS impersonation only (no clearance cookie)
    try:
        from curl_cffi.requests import Session as CurlSession  # type: ignore

        session = CurlSession(impersonate=impersonate)
        session.headers.update({**_BROWSER_HEADERS, **_api_headers()})
        logger.info(
            "Using curl_cffi TLS impersonation (%s, no CF clearance cookie). "
            "Set FLARESOLVERR_URL or CF_CLEARANCE if you get 403 errors.",
            impersonate,
        )
        return session  # type: ignore[return-value]
    except ImportError:
        pass

    # Strategy 4: plain requests – will get 403 but allow unit tests to inject mocks
    logger.warning(
        "No Cloudflare bypass configured. "
        "API calls will likely fail with HTTP 403. "
        "Set FLARESOLVERR_URL (recommended) or CF_CLEARANCE+CF_USER_AGENT "
        "environment variables to enable scraping."
    )
    session = requests.Session()
    session.headers.update({**_BROWSER_HEADERS, **_api_headers()})
    return session


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------


class KRuokaScraper:
    """Scraper for the k-ruoka.fi product catalogue.

    Parameters
    ----------
    store_id:
        The K-group store identifier (e.g. ``"N110"`` for K-Supermarket Helsinki,
        ``"N137"`` for K-Citymarket Tammisto).  The store ID appears in the URL
        after selecting a store on k-ruoka.fi (``?storeId=…``).  Run with
        ``--list-stores`` to discover available store IDs.
    session:
        An optional :class:`requests.Session` (or ``curl_cffi`` session) to
        reuse.  When ``None``, a new session is created via :func:`_make_session`
        which tries all available Cloudflare bypass strategies.
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
        self._session = session if session is not None else _make_session()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def search(self, query: str, max_products: Optional[int] = None) -> Iterator[Product]:
        """Yield products whose name or description matches *query*.

        Uses ``POST /kr-api/v2/product-search/{query}`` with ``storeId``,
        ``limit``, ``offset``, and ``language`` as query-string parameters.

        Parameters
        ----------
        query:
            Free-text search term (Finnish or English).
        max_products:
            Stop after this many products.  ``None`` means no limit.
        """
        yield from self._paginate_search(query, max_products)

    def browse(self, max_products: Optional[int] = None) -> Iterator[Product]:
        """Yield all available products in the store catalogue.

        First fetches the list of offer categories, then iterates through each
        category fetching products page by page.

        Parameters
        ----------
        max_products:
            Stop after this many products.  ``None`` means no limit.
        """
        yield from self._paginate_browse(max_products)

    # ------------------------------------------------------------------
    # Search pagination
    # ------------------------------------------------------------------

    def _paginate_search(
        self, query: str, max_products: Optional[int]
    ) -> Iterator[Product]:
        """Paginate through product-search results."""
        offset = 0
        total_yielded = 0
        path = _SEARCH_PATH.format(query=query)

        while True:
            params = {
                "storeId": self.store_id,
                "limit": _SEARCH_PAGE_SIZE,
                "offset": offset,
                "language": "fi",
                "discountFilter": False,
                "isTosTrOffer": False,
            }
            url = self._api_url(path) + "?" + urlencode(
                {k: v for k, v in params.items() if v is not False}
            )
            data = self._post_json(url, payload=None)
            if data is None:
                break

            items = self._extract_search_results(data)
            if not items:
                logger.debug("No more search results at offset=%d.", offset)
                break

            for item in items:
                product = self._parse_search_product(item)
                if product is None:
                    continue
                yield product
                total_yielded += 1
                if max_products is not None and total_yielded >= max_products:
                    return

            total = self._extract_total(data)
            offset += _SEARCH_PAGE_SIZE
            if total is not None and offset >= total:
                break

            time.sleep(self.request_delay)

    # ------------------------------------------------------------------
    # Browse pagination
    # ------------------------------------------------------------------

    def _paginate_browse(self, max_products: Optional[int]) -> Iterator[Product]:
        """Fetch all products by iterating offer categories."""
        categories = self._fetch_offer_categories()
        total_yielded = 0

        for category in categories:
            slug = category.get("slug", "")
            if not slug:
                continue
            for product in self._paginate_offer_category(slug):
                yield product
                total_yielded += 1
                if max_products is not None and total_yielded >= max_products:
                    return
            time.sleep(self.request_delay)

    def _fetch_offer_categories(self) -> list[dict]:
        """Return the list of offer category objects for this store."""
        url = self._api_url(_OFFER_CATEGORIES_PATH)
        data = self._post_json(url, {"storeId": self.store_id})
        if data is None:
            return []
        categories = data.get("offerCategories", [])
        logger.debug("Found %d offer categories.", len(categories))
        return categories

    def _paginate_offer_category(self, slug: str) -> Iterator[Product]:
        """Paginate through all products in a single offer category."""
        offset = 0
        total_hits: Optional[int] = None
        url = self._api_url(_OFFER_CATEGORY_PATH)

        while True:
            payload = {
                "storeId": self.store_id,
                "category": {"kind": "productCategory", "slug": slug},
                "offset": offset,
                "limit": _OFFER_CATEGORY_PAGE_SIZE,
                "pricing": {},
            }
            data = self._post_json(url, payload)
            if data is None:
                break

            if total_hits is None:
                total_hits = data.get("totalHits", 0)

            offers = data.get("offers", [])
            if not offers:
                break

            for offer in offers:
                product = self._parse_offer_product(offer)
                if product is None:
                    continue
                yield product

            offset += _OFFER_CATEGORY_PAGE_SIZE
            if total_hits is not None and offset >= total_hits:
                break

            time.sleep(self.request_delay)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        return f"{_BASE_URL}{_KR_API}/{path}"

    def _post_json(self, url: str, payload: Optional[dict]) -> Optional[dict]:
        """Perform a POST request and return parsed JSON, or ``None`` on failure."""
        try:
            resp = self._session.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.error(
                "HTTP error %s for %s: %s",
                exc.response.status_code,
                url,
                exc,
            )
        except requests.RequestException as exc:
            logger.error("Request failed for %s: %s", url, exc)
        except ValueError as exc:
            logger.error("Failed to decode JSON from %s: %s", url, exc)
        return None

    # ------------------------------------------------------------------
    # Data extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_search_results(data: dict | list) -> list:
        """Return the list of product dicts from a product-search response."""
        if isinstance(data, list):
            return data
        for key in ("results", "products", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    @staticmethod
    def _extract_total(data: dict) -> Optional[int]:
        """Return the total number of available items, if present."""
        for key in ("totalHits", "total", "totalCount", "count"):
            if isinstance(data.get(key), int):
                return data[key]
        return None

    @staticmethod
    def _parse_search_product(item: dict) -> Optional[Product]:
        """Parse a product from a ``/v2/product-search`` result item.

        Expected shape::

            {
              "id": "6418248002382",
              "ean": "6418248002382",
              "localizedName": {"finnish": "Maito 1l", ...},
              "imageUrl": "https://...",
              ...
            }
        """
        # Name: prefer localizedName.finnish, fall back to generic name keys
        localized = item.get("localizedName") or {}
        name: str = (
            (localized.get("finnish") or localized.get("fi") or "")
            or item.get("name")
            or item.get("title")
            or item.get("productName")
            or ""
        ).strip()

        # EAN: the field is usually "ean" or the product ID equals the EAN
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

        product_id = str(item.get("id") or item.get("productId") or "")

        images = item.get("images") or []
        image_url = (
            item.get("imageUrl")
            or item.get("image")
            or (images[0] if images else "")
        )

        description = (item.get("description") or "").strip()

        return Product(
            name=name,
            ean=ean,
            product_id=product_id,
            description=description,
            image_url=image_url,
            extra=item,
        )

    @staticmethod
    def _parse_offer_product(offer: dict) -> Optional[Product]:
        """Parse a product from an ``/offer-category`` offer item.

        Expected shape (simplified)::

            {
              "id": "S4177155P",
              "product": {
                "id": "6418248002382",
                "product": {
                  "ean": "6418248002382",
                  "localizedName": {"finnish": "Suvi porkkanasose 1kg", ...},
                  "images": ["https://..."],
                  "productAttributes": {"ean": "6418248002382", ...}
                }
              }
            }
        """
        # Navigate to the inner product dict
        outer = offer.get("product") or {}
        inner = outer.get("product") or outer

        if not inner:
            return None

        localized = inner.get("localizedName") or {}
        name: str = (
            (localized.get("finnish") or localized.get("fi") or "")
            or inner.get("name")
            or offer.get("title")
            or ""
        ).strip()

        attrs = inner.get("productAttributes") or {}
        ean: str = (
            inner.get("ean")
            or attrs.get("ean")
            or inner.get("baseEan")
            or outer.get("ean")
            or ""
        ).strip()

        if not name and not ean:
            return None

        product_id = str(
            inner.get("id") or outer.get("id") or offer.get("id") or ""
        )

        images = inner.get("images") or []
        image_url = (
            inner.get("imageUrl")
            or (images[0] if images else "")
            or offer.get("image")
            or ""
        )

        label_name = attrs.get("labelName") or {}
        description = (
            label_name.get("fi") or label_name.get("en") or ""
        ).strip()

        return Product(
            name=name,
            ean=ean,
            product_id=product_id,
            description=description,
            image_url=image_url,
            extra=offer,
        )
