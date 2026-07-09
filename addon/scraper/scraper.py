"""Scraper for https://www.k-ruoka.fi/kauppa.

Fetches product listings from the k-ruoka.fi API and yields :class:`Product`
objects that contain the product name and EAN barcode.

Two backends are available:

1. **GraphQL** (default, ``use_graphql=True``):
   Endpoint ``https://mobile.k-ruoka.fi/graphql`` — the official mobile app
   GraphQL API.  Accessible with a standard mobile User-Agent and **no
   Cloudflare bypass required**.  Supports both keyword search and full
   catalogue browsing via category slugs.  Maximum 100 results per page;
   hard server-side limit of offset ≤ 1000 per query.

2. **kr-api REST** (fallback, ``use_graphql=False``):
   Endpoint ``https://www.k-ruoka.fi/kr-api`` — the internal REST API used by
   the web SPA.  Requires a valid Cloudflare ``cf_clearance`` cookie because the
   site uses Cloudflare Bot Management.  Bypass strategies (tried in order):

   a. **FlareSolverr** — set ``FLARESOLVERR_URL`` env var::

          docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest

   b. **Manual cookies** — set ``CF_CLEARANCE`` and ``CF_USER_AGENT`` env vars
      with values from your browser's DevTools after visiting
      https://www.k-ruoka.fi/kauppa.

   c. **curl_cffi** TLS impersonation (no clearance cookie; may work on some CF
      tiers).  Install with ``pip install curl_cffi``.

Relevant environment variables
-------------------------------
``FLARESOLVERR_URL``
    URL of a running FlareSolverr instance (kr-api only).
``CF_CLEARANCE``
    Value of the ``cf_clearance`` cookie (kr-api only).
``CF_USER_AGENT``
    Browser User-Agent matching the ``CF_CLEARANCE`` cookie (kr-api only).
``K_BUILD_NUMBER``
    Override the ``x-k-build-number`` header (kr-api only; update when stale).
``K_EXPERIMENTS``
    Override the ``x-k-experiments`` header (kr-api only; update when stale).
``CURL_CFFI_IMPERSONATE``
    Browser profile for curl_cffi TLS impersonation (default: ``chrome124``).
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL backend constants (mobile.k-ruoka.fi)
# ---------------------------------------------------------------------------

_MOBILE_GRAPHQL_URL = "https://mobile.k-ruoka.fi/graphql"
_MOBILE_UA = "K-Ruoka/1.0.0 (iPhone; iOS 14.4.2; Scale/3.00)"

# Maximum results per GraphQL request (server returns 500 for values > 100).
_GRAPHQL_PAGE_SIZE = 100

# Server returns 500 for offset >= 1100; stay safely below that limit.
_GRAPHQL_MAX_OFFSET = 1000

# GraphQL query used for both search and browse.
_GQL_PRODUCT_SEARCH = """\
query productAndAssortmentSearchV2(
  $query: String!
  $storeId: String!
  $limit: Float!
  $offset: Float!
  $categoryPath: String
) {
  productAndAssortmentSearchV2(
    query: $query
    storeId: $storeId
    limit: $limit
    offset: $offset
    categoryPath: $categoryPath
  ) {
    results {
      ... on Product {
        id
        ean
        imageUrl
        productType
        localizedName { finnish }
        __typename
      }
      ... on AssortmentSearchResult {
        id
        eans
        imageUrl
        productType
        localizedName { finnish }
        __typename
      }
    }
    totalHits
    __typename
  }
}
"""

# Stable K-group product category slugs used for full-catalogue browsing.
# Sourced from kr-api/offer-categories; these slugs are consistent across stores.
_PRODUCT_CATEGORY_SLUGS: list[str] = [
    "hedelmat-ja-vihannekset",
    "leivat-keksit-ja-leivonnaiset",
    "liha-ja-kasviproteiinit",
    "kala-ja-merenelavat",
    "valmisruoka",
    "maito-juusto-munat-ja-rasvat",
    "kuivat-elintarvikkeet-ja-leivonta",
    "sailykkeet-keitot-ja-ateria-ainekset",
    "oljyt-etikat-ja-salaattikastikkeet",
    "mausteet-ja-maustaminen",
    "texmex-ja-maailman-maut",
    "pakasteet",
    "makeiset-ja-naposteltavat",
    "juomat",
    "lapset",
    "lemmikit",
    "kosmetiikka-terveys-ja-hygienia",
    "keittio-astiat-ja-kattaus",
    "kodinhoito-ja-taloustarvikkeet",
    "kodintekstiilit-ja-sisustus",
    "kodinkoneet-ja-elektroniikka",
    "sahko-pienrauta-ja-autotarvikkeet",
    "kukat-ja-puutarha",
    "vapaa-aika-ja-urheilu",
    "kirjat-lehdet-ja-paperitarvikkeet",
    "kengat-ja-kenkienhoito",
    "vaatteet-ja-asusteet",
]

# ---------------------------------------------------------------------------
# kr-api REST backend constants (www.k-ruoka.fi/kr-api)
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.k-ruoka.fi"
_KR_API = "/kr-api"

_SEARCH_PATH = "v2/product-search/{query}"
_OFFER_CATEGORIES_PATH = "offer-categories"
_OFFER_CATEGORY_PATH = "offer-category"

_SEARCH_PAGE_SIZE = 100
_OFFER_CATEGORY_PAGE_SIZE = 25  # API returns 400 for values > 25

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
_DEFAULT_IMPERSONATE = "chrome124"

# Courtesy delay between requests.
_REQUEST_DELAY = 0.5

# Global pacing for the kr-api backend.  The v2/product-search endpoint
# rate-limits aggressively (HTTP 429) when hit back-to-back for several
# stores per EAN across a whole catalogue update, so all kr-api requests —
# across every KRuokaScraper instance — share one minimum interval.
_KRAPI_MIN_INTERVAL = float(os.environ.get("KRAPI_MIN_INTERVAL", "2.0"))
_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_BACKOFF = 5.0  # base backoff (s) when no Retry-After header
# Cloudflare sometimes demands hours via Retry-After; sleeping that long
# would stall a whole catalogue run, so cap and give up via retries instead.
_RATE_LIMIT_BACKOFF_MAX = 120.0
# Circuit breaker: after this many consecutive requests exhaust their 429
# retries, stop hitting kr-api entirely for the cooldown period.  Hammering
# an already-throttled edge is what escalates 429s into real blocks.
_KRAPI_CIRCUIT_THRESHOLD = 3
_KRAPI_CIRCUIT_COOLDOWN = float(os.environ.get("KRAPI_CIRCUIT_COOLDOWN", "900"))


class _KrApiThrottle:
    """Process-wide request pacing with 429 penalty support (thread-safe).

    Each gap is jittered (0.7–1.5× the base interval) so the traffic doesn't
    have a machine-perfect cadence.
    """

    def __init__(self, min_interval: float = _KRAPI_MIN_INTERVAL) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        """Block until the next request slot, then claim the one after it."""
        interval = self._min_interval * random.uniform(0.7, 1.5)
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + interval
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds: float) -> None:
        """Push back the next allowed request (server asked us to back off)."""
        with self._lock:
            self._next_allowed = max(
                self._next_allowed, time.monotonic() + seconds
            )


_krapi_throttle = _KrApiThrottle()


class _KrApiCircuit:
    """Trip after consecutive rate-limit failures; auto-close after cooldown."""

    def __init__(
        self,
        threshold: int = _KRAPI_CIRCUIT_THRESHOLD,
        cooldown: float = _KRAPI_CIRCUIT_COOLDOWN,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._consecutive = 0
        self._open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._consecutive = 0

    def record_failure(self) -> None:
        """Count one request that exhausted all its 429 retries."""
        with self._lock:
            self._consecutive += 1
            if (
                self._consecutive >= self._threshold
                and time.monotonic() >= self._open_until
            ):
                self._open_until = time.monotonic() + self._cooldown
                logger.warning(
                    "kr-api rate limited %d times in a row — pausing all "
                    "kr-api requests for %.0f min (prices/availability from "
                    "kr-api will be skipped; GraphQL lookups continue).",
                    self._consecutive, self._cooldown / 60,
                )


_krapi_circuit = _KrApiCircuit()


def _retry_after_seconds(response, attempt: int) -> float:
    """Backoff duration for a 429: Retry-After header, else exponential."""
    try:
        header = (response.headers or {}).get("Retry-After", "")
        if header:
            return min(max(float(header), 1.0), _RATE_LIMIT_BACKOFF_MAX)
    except (AttributeError, TypeError, ValueError):
        pass
    return min(_RATE_LIMIT_BACKOFF * (2 ** attempt), _RATE_LIMIT_BACKOFF_MAX)


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
    # Store-specific consumer price (VAT incl., EUR) and the comparison price
    # (e.g. €/l or €/kg).  Only the kr-api REST backend exposes pricing, via the
    # per-product ``mobilescan`` block; the GraphQL backend leaves these ``None``.
    price: Optional[float] = None
    comparison_price: Optional[float] = None
    comparison_unit: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class StoreAvailability:
    """Assortment-level availability of one product at one store.

    ``available`` means the product appears in the store's store-scoped
    search — it says the store *carries* the product, not that shelves are
    stocked right now.  ``price`` is only filled by the kr-api backend.
    """

    store_id: str
    available: bool
    price: Optional[float] = None
    price_currency: str = "EUR"


def _parse_mobilescan_pricing(
    product: dict,
) -> tuple[Optional[float], Optional[float], str]:
    """Extract the normal (non-discount) price from a kr-api ``mobilescan`` block.

    The kr-api ``v2/product-search`` / product-set endpoints attach a
    store-specific ``mobilescan.pricing.normal`` object::

        "mobilescan": {"pricing": {"normal": {
            "price": 1.54,
            "unitPrice": {"value": 1.54, "unit": "l", "contentSize": 1}
        }}, "vat": 13.5}

    Returns ``(price, comparison_price, comparison_unit)``; any missing value is
    ``None`` / ``""``.  The regular price is used (not ``discount``) so the
    stored value is stable across short-lived campaigns.
    """
    pricing = (product.get("mobilescan") or {}).get("pricing") or {}
    normal = pricing.get("normal") or {}
    price = normal.get("price")
    unit_price = normal.get("unitPrice") or {}
    comp = unit_price.get("value")
    comp_unit = unit_price.get("unit") or ""
    return (
        float(price) if isinstance(price, (int, float)) else None,
        float(comp) if isinstance(comp, (int, float)) else None,
        str(comp_unit),
    )


def _normalize_store_name(name: str) -> str:
    """Normalize Unicode punctuation the kr-api uses in store names.

    ``kr-api/store/{id}`` returns names using the Unicode NON-BREAKING HYPHEN
    (U+2011, e.g. ``"K‑Citymarket Pirkkala"``) instead of an ASCII hyphen.
    Downstream, HA-stock shortens chip labels by matching the ASCII prefix
    ``"K-Citymarket "``, so these characters must be normalized to their
    ASCII equivalents. Also normalizes the HYPHEN (U+2010) and NO-BREAK SPACE
    (U+00A0) characters that appear in the same API responses.
    """
    return (
        (name or "")
        .replace("‑", "-")
        .replace("‐", "-")
        .replace(" ", " ")
        .strip()
    )


def _normalize_ean(ean: str) -> str:
    """Normalize an EAN/GTIN code by stripping spurious leading zeros.

    The k-ruoka.fi API sometimes returns EAN-8 codes left-padded with zeros
    to 13 digits (e.g. ``"0000090493508"`` for the EAN-8 ``"90493508"``),
    which breaks downstream lookups. This helper strips leading zeros down
    to the smallest standard GTIN length (8, 12, 13 or 14 digits).
    Non-numeric or empty inputs are returned unchanged (after stripping
    whitespace).
    """
    ean = (ean or "").strip()
    if not ean or not ean.isdigit():
        return ean
    stripped = ean.lstrip("0")
    if not stripped:
        return ean
    for length in (8, 12, 13, 14):
        if len(stripped) <= length <= len(ean):
            return stripped.zfill(length)
    return stripped


# ---------------------------------------------------------------------------
# Session factories
# ---------------------------------------------------------------------------

def _make_graphql_session() -> requests.Session:
    """Create a session for ``mobile.k-ruoka.fi/graphql``.

    No Cloudflare bypass is required — the mobile GraphQL API accepts requests
    with a standard mobile User-Agent.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": _MOBILE_UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    return session


# CF clearance cookies are typically valid ~30 min; persist them so runs
# started within that window skip the FlareSolverr solve entirely.
_CF_CACHE_TTL = float(os.environ.get("CF_CLEARANCE_TTL", "1500"))  # 25 min


def _cf_cache_path() -> Path:
    return Path(os.environ.get("SCRAPER_STATE_DIR", "/data")) / "cf_clearance.json"


def _load_cached_cf() -> Optional[tuple[dict, str]]:
    """Return (cookies, user_agent) from the on-disk cache, or None."""
    try:
        data = json.loads(_cf_cache_path().read_text())
        if time.time() - float(data["saved_at"]) < _CF_CACHE_TTL:
            return dict(data["cookies"]), str(data["user_agent"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _save_cached_cf(cookies: dict, user_agent: str) -> None:
    """Persist the CF solution (best-effort — never raises)."""
    try:
        _cf_cache_path().write_text(json.dumps({
            "cookies": cookies,
            "user_agent": user_agent,
            "saved_at": time.time(),
        }))
    except OSError:
        pass


def _flaresolverr_endpoint(base_url: str) -> str:
    """Return the FlareSolverr API endpoint for a configured base URL.

    The API lives at ``/v1``; the service root only accepts GET (POST gets
    HTTP 405), so accept both ``http://host:8191`` and ``…:8191/v1``.
    """
    base = base_url.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _resolve_cf_flaresolverr(site_url: str) -> tuple[dict, str]:
    """Obtain Cloudflare clearance cookies via a local FlareSolverr instance."""
    flaresolverr_url = _flaresolverr_endpoint(os.environ["FLARESOLVERR_URL"])
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
        raise RuntimeError("FlareSolverr did not return cf_clearance cookie")
    logger.info(
        "FlareSolverr: obtained %d cookies, UA=%s…", len(cookies), user_agent[:60]
    )
    return cookies, user_agent


def _build_krapi_session(cf_cookies: dict, user_agent: str) -> requests.Session:
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


# One CF clearance is valid for every kr-api session in this process, so
# resolve it once and share — an --update run builds one scraper per store
# and would otherwise hit FlareSolverr once per store.
_cf_solution_lock = threading.Lock()
_cf_solution: Optional[tuple[dict, str]] = None


def _make_krapi_session() -> requests.Session:
    """Build an authenticated kr-api session, trying all CF bypass strategies.

    Strategy order:

    1. FlareSolverr (if ``FLARESOLVERR_URL`` env var is set).
    2. Pre-supplied cookies (if ``CF_CLEARANCE`` env var is set).
    3. curl_cffi TLS impersonation without cookies.
    4. Plain ``requests.Session`` (will likely get HTTP 403).
    """
    global _cf_solution
    site_url = f"{_BASE_URL}/kauppa"
    impersonate = os.environ.get("CURL_CFFI_IMPERSONATE", _DEFAULT_IMPERSONATE)

    if os.environ.get("FLARESOLVERR_URL"):
        try:
            with _cf_solution_lock:
                if _cf_solution is None:
                    _cf_solution = _load_cached_cf()
                    if _cf_solution is not None:
                        logger.info("Reusing cached CF clearance from disk.")
                if _cf_solution is None:
                    _cf_solution = _resolve_cf_flaresolverr(site_url)
                    _save_cached_cf(*_cf_solution)
                cookies, ua = _cf_solution
            return _build_krapi_session(cookies, ua)
        except Exception as exc:
            logger.warning("FlareSolverr bypass failed: %s", exc)

    cf_clearance = os.environ.get("CF_CLEARANCE")
    if cf_clearance:
        cf_ua = os.environ.get("CF_USER_AGENT", "")
        logger.info("Using CF_CLEARANCE cookie from environment.")
        return _build_krapi_session({"cf_clearance": cf_clearance}, cf_ua)

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

    logger.warning(
        "No Cloudflare bypass configured. "
        "kr-api calls will likely fail with HTTP 403. "
        "Set FLARESOLVERR_URL (recommended) or CF_CLEARANCE+CF_USER_AGENT "
        "environment variables, or use the GraphQL backend (use_graphql=True)."
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
        after selecting a store on k-ruoka.fi (``?storeId=…``).
    session:
        An optional :class:`requests.Session` to reuse.  When ``None``, a new
        session is created automatically based on ``use_graphql``.
    request_delay:
        Seconds to wait between consecutive HTTP requests (default 0.5 s).
    use_graphql:
        When ``True`` (default) use the mobile GraphQL API at
        ``mobile.k-ruoka.fi/graphql`` — no Cloudflare bypass needed.
        When ``False`` use the kr-api REST backend at
        ``www.k-ruoka.fi/kr-api`` — requires a CF bypass (see module docstring).
    """

    def __init__(
        self,
        store_id: str,
        session: Optional[requests.Session] = None,
        request_delay: float = _REQUEST_DELAY,
        use_graphql: bool = True,
    ) -> None:
        # store_id may be a single id ("N110") or a comma-separated list
        # ("N110,K532,..."). search() tries each store in order with fallback.
        self.store_ids = [s.strip() for s in str(store_id).split(",") if s.strip()] or [str(store_id)]
        self.store_id = self.store_ids[0]
        self.request_delay = request_delay
        self._use_graphql = use_graphql
        if session is not None:
            self._session = session
        elif use_graphql:
            self._session = _make_graphql_session()
        else:
            self._session = _make_krapi_session()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, max_products: Optional[int] = None) -> Iterator[Product]:
        """Search the configured store(s) for *query*.

        ``store_id`` may be a single id or a comma-separated list. With multiple
        stores, each is tried in order and the results from the FIRST store that
        returns any match are yielded (fallback for products not stocked at every
        store; avoids cross-store duplicates). Most products exist at all stores,
        so the first store usually answers.
        """
        original = self.store_id
        try:
            for store_id in self.store_ids:
                self.store_id = store_id
                found = False
                for product in self._search_one_store(query, max_products):
                    found = True
                    yield product
                if found:
                    return
        finally:
            self.store_id = original

    def _search_one_store(self, query: str, max_products: Optional[int] = None) -> Iterator[Product]:
        """Yield products whose name matches *query* (case-insensitive).

        The upstream API performs broad full-text matching that can return
        unrelated products (e.g. searching "kevytmaito" also returns
        "maitosuklaa").  Results are therefore filtered client-side so that
        only products whose name contains every word of the query are yielded.
        Words can appear in any order and need not be contiguous, so
        ``"lotus paperi"`` matches ``"Lotus Soft Embo 8 rll wc-paperi"``.

        For multi-word queries the *first* word is sent to the upstream API
        (the API may not match non-contiguous words) and the remaining words
        are matched client-side.

        Uses the GraphQL backend by default (``use_graphql=True``).  Each page
        returns up to 100 results; the server stops serving results at
        ``offset=1000`` so a single search yields at most 1,100 products.

        Parameters
        ----------
        query:
            Free-text search term (Finnish or English).
        max_products:
            Stop after this many products.  ``None`` means no limit.
        """
        # When the query looks like a barcode (digits only), skip the name
        # filter — the API will return matching products by EAN which won't
        # contain the numeric string in their name.
        if query.isdigit():
            if self._use_graphql:
                yield from self._paginate_graphql(query=query, max_products=max_products)
            else:
                yield from self._paginate_search(query, max_products)
            return

        words = query.lower().split()

        # For multi-word queries, send only the first word to the upstream API
        # so that the result set is broad enough for client-side filtering to
        # match products where the query words appear non-contiguously
        # (e.g. "lotus paperi" → API query "lotus", then filter for "paperi").
        is_multi = len(words) > 1
        api_query = words[0] if is_multi else query
        # Don't cap API results for multi-word queries — the client-side
        # filter will discard many rows, so we need a larger pool.
        api_limit = None if is_multi else max_products

        if self._use_graphql:
            results = self._paginate_graphql(query=api_query, max_products=api_limit)
        else:
            results = self._paginate_search(api_query, api_limit)

        yielded = 0
        for product in results:
            name_lower = product.name.lower()
            if all(w in name_lower for w in words):
                yield product
                yielded += 1
                if max_products is not None and yielded >= max_products:
                    return

    def check_store_availability(self, ean: str) -> list[StoreAvailability]:
        """Check every configured store for *ean* (assortment sweep).

        Unlike :meth:`search`, this does NOT stop at the first hit — it asks
        each store in ``self.store_ids`` in turn.  Stores whose lookup errors
        (CF wall, timeout, HTTP error) are OMITTED from the result so the
        caller never records a false negative.
        """
        target = _normalize_ean(ean)
        if not target:
            return []
        results: list[StoreAvailability] = []
        original = self.store_id
        try:
            for index, store_id in enumerate(self.store_ids):
                if index:
                    time.sleep(self.request_delay)
                self.store_id = store_id
                entry = self._check_one_store(target)
                if entry is not None:
                    results.append(entry)
        finally:
            self.store_id = original
        return results

    def _check_one_store(self, target: str) -> Optional[StoreAvailability]:
        """One-page EAN lookup at the current store.

        Returns ``None`` when the request failed (indeterminate — caller
        omits the store), otherwise a :class:`StoreAvailability`.
        """
        if self._use_graphql:
            data = self._post_graphql({
                "query": target,
                "storeId": self.store_id,
                "limit": float(_GRAPHQL_PAGE_SIZE),
                "offset": 0.0,
            })
            if data is None:
                return None
            items = (
                (data.get("data") or {})
                .get("productAndAssortmentSearchV2") or {}
            ).get("results") or []
            for item in items:
                eans = {_normalize_ean(str(e)) for e in (item.get("eans") or [])}
                eans.add(_normalize_ean(str(item.get("ean") or "")))
                if target in eans:
                    return StoreAvailability(store_id=self.store_id, available=True)
            return StoreAvailability(store_id=self.store_id, available=False)

        # kr-api backend — price rides along via the mobilescan block.
        path = _SEARCH_PATH.format(query=target)
        params = {
            "storeId": self.store_id,
            "limit": _SEARCH_PAGE_SIZE,
            "offset": 0,
            "language": "fi",
            "discountFilter": False,
            "isTosTrOffer": False,
        }
        url = self._api_url(path) + "?" + urlencode(
            {k: v for k, v in params.items() if v is not False}
        )
        data = self._post_json(url, payload=None)
        if data is None:
            return None
        for item in self._extract_search_results(data):
            product = self._parse_search_product(item)
            if product is not None and product.ean == target:
                return StoreAvailability(
                    store_id=self.store_id,
                    available=True,
                    price=product.price,
                )
        return StoreAvailability(store_id=self.store_id, available=False)

    def fetch_store_name(self, store_id: str) -> Optional[str]:
        """Resolve a K-store ID to its display name via ``kr-api/store/{id}``.

        Only works on the kr-api backend (needs the CF-bypassed session);
        the GraphQL backend returns ``None`` and callers fall back to the
        raw store ID.  The endpoint returns a single JSON dict (not a list)
        with ``name`` (falling back to ``shortName``); Unicode punctuation
        in the name (non-breaking hyphen, hyphen, no-break space) is
        normalized to ASCII so downstream chip-shortening (HA-stock) still
        matches on the ASCII ``"K-Citymarket "`` prefix.
        """
        if self._use_graphql:
            return None
        data = self._get_json(self._api_url(f"store/{store_id}"))
        if not isinstance(data, dict):
            return None
        name = _normalize_store_name(data.get("name") or data.get("shortName") or "")
        return name or None

    def browse(self, max_products: Optional[int] = None) -> Iterator[Product]:
        """Yield all available products in the store catalogue.

        With the GraphQL backend (default) the catalogue is browsed category
        by category using ``_PRODUCT_CATEGORY_SLUGS``, deduplicating by EAN.
        Because the server enforces an ``offset ≤ 1000`` limit, large categories
        (> 1,100 products) will be partially fetched; most food categories are
        well below this threshold.

        With the kr-api backend the catalogue is browsed via
        ``/offer-categories`` + ``/offer-category`` pagination.

        Parameters
        ----------
        max_products:
            Stop after this many products.  ``None`` means no limit.
        """
        if self._use_graphql:
            yield from self._browse_graphql(max_products)
        else:
            yield from self._paginate_browse(max_products)

    # ------------------------------------------------------------------
    # GraphQL backend – search & browse
    # ------------------------------------------------------------------

    def _paginate_graphql(
        self,
        query: str,
        category_path: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Iterator[Product]:
        """Paginate through GraphQL ``productAndAssortmentSearchV2`` results."""
        offset = 0
        total_yielded = 0

        while offset <= _GRAPHQL_MAX_OFFSET:
            variables: dict = {
                "query": query,
                "storeId": self.store_id,
                "limit": float(_GRAPHQL_PAGE_SIZE),
                "offset": float(offset),
            }
            if category_path is not None:
                variables["categoryPath"] = category_path

            data = self._post_graphql(variables)
            if data is None:
                break

            search_data = (
                (data.get("data") or {})
                .get("productAndAssortmentSearchV2") or {}
            )
            results = search_data.get("results") or []
            if not results:
                logger.debug(
                    "No GraphQL results at offset=%d (query=%r, category=%r).",
                    offset, query, category_path,
                )
                break

            for item in results:
                product = self._parse_graphql_result(item)
                if product is None:
                    continue
                yield product
                total_yielded += 1
                if max_products is not None and total_yielded >= max_products:
                    return

            total_hits = search_data.get("totalHits")
            offset += _GRAPHQL_PAGE_SIZE

            # Stop if this was the last (partial) page or we have all results.
            if len(results) < _GRAPHQL_PAGE_SIZE:
                break
            if total_hits is not None and offset >= total_hits:
                break

            time.sleep(self.request_delay)

    def _browse_graphql(self, max_products: Optional[int]) -> Iterator[Product]:
        """Browse all products category by category via GraphQL.

        Iterates :data:`_PRODUCT_CATEGORY_SLUGS`, fetching up to 1,100 products
        per category, and deduplicates results by EAN across categories.
        """
        seen_eans: set[str] = set()
        total_yielded = 0

        for slug in _PRODUCT_CATEGORY_SLUGS:
            logger.debug("Browsing category: %s", slug)
            for product in self._paginate_graphql(
                query="", category_path=slug
            ):
                if product.ean and product.ean in seen_eans:
                    continue
                if product.ean:
                    seen_eans.add(product.ean)
                yield product
                total_yielded += 1
                if max_products is not None and total_yielded >= max_products:
                    return
            time.sleep(self.request_delay)

    def _post_graphql(self, variables: dict) -> Optional[dict]:
        """POST a GraphQL query and return the response, or ``None`` on error."""
        payload = {
            "operationName": "productAndAssortmentSearchV2",
            "variables": variables,
            "query": _GQL_PRODUCT_SEARCH,
        }
        try:
            resp = self._session.post(
                _MOBILE_GRAPHQL_URL, json=payload, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                logger.warning("GraphQL errors: %s", data["errors"])
                return None
            return data
        except requests.HTTPError as exc:
            logger.error(
                "HTTP error %s for GraphQL: %s", exc.response.status_code, exc
            )
        except requests.RequestException as exc:
            logger.error("Request failed for GraphQL: %s", exc)
        except ValueError as exc:
            logger.error("Failed to decode GraphQL response: %s", exc)
        return None

    @staticmethod
    def _parse_graphql_result(item: dict) -> Optional[Product]:
        """Parse a GraphQL ``Product`` or ``AssortmentSearchResult`` item.

        ``Product`` items carry a single ``ean`` string.
        ``AssortmentSearchResult`` items carry a list ``eans``; the first entry
        is used as the canonical EAN.

        Expected ``Product`` shape::

            {
              "__typename": "Product",
              "id": "6410405082657",
              "ean": "6410405082657",
              "localizedName": {"finnish": "Pirkka suomalainen kevytmaito 1l"},
              "imageUrl": "https://public.keskofiles.com/...",
              "productType": "NORMAL"
            }

        Expected ``AssortmentSearchResult`` shape::

            {
              "__typename": "AssortmentSearchResult",
              "id": "ASSORT_123",
              "eans": ["6410405082657", "6410405082664"],
              "localizedName": {"finnish": "Pirkka maito"},
              "imageUrl": "https://..."
            }
        """
        localized = item.get("localizedName") or {}
        name: str = (
            localized.get("finnish") or localized.get("fi") or ""
        ).strip()

        typename = item.get("__typename", "")
        if typename == "AssortmentSearchResult":
            eans = item.get("eans") or []
            ean: str = _normalize_ean(str(eans[0])) if eans else ""
        else:
            ean = _normalize_ean(str(item.get("ean") or ""))

        if not name and not ean:
            return None

        return Product(
            name=name,
            ean=ean,
            product_id=str(item.get("id") or ""),
            image_url=item.get("imageUrl") or "",
            extra=item,
        )

    # ------------------------------------------------------------------
    # kr-api REST backend – search & browse
    # ------------------------------------------------------------------

    def _paginate_search(
        self, query: str, max_products: Optional[int]
    ) -> Iterator[Product]:
        """Paginate through kr-api ``/v2/product-search`` results."""
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

    def _paginate_browse(self, max_products: Optional[int]) -> Iterator[Product]:
        """Browse all products via kr-api offer categories."""
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
        """Paginate through all products in a single kr-api offer category."""
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
    # kr-api HTTP helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        return f"{_BASE_URL}{_KR_API}/{path}"

    def _request_json(
        self, method: str, url: str, payload: Optional[dict] = None
    ) -> Optional[dict]:
        """Perform a throttled kr-api request and return parsed JSON.

        All kr-api requests share the global :data:`_krapi_throttle` pacing.
        HTTP 429 responses are retried with Retry-After/exponential backoff
        up to :data:`_RATE_LIMIT_MAX_RETRIES` times; any other failure
        returns ``None`` as before.
        """
        if _krapi_circuit.is_open():
            logger.debug("kr-api circuit open — skipping %s", url)
            return None
        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            _krapi_throttle.wait()
            try:
                if method == "GET":
                    resp = self._session.get(url, timeout=15)
                else:
                    resp = self._session.post(url, json=payload, timeout=15)
                resp.raise_for_status()
                _krapi_circuit.record_success()
                return resp.json()
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                if status == 429 and attempt < _RATE_LIMIT_MAX_RETRIES:
                    backoff = _retry_after_seconds(exc.response, attempt)
                    logger.warning(
                        "HTTP 429 for %s — backing off %.1fs (retry %d/%d)",
                        url, backoff, attempt + 1, _RATE_LIMIT_MAX_RETRIES,
                    )
                    _krapi_throttle.penalize(backoff)
                    continue
                if status == 429:
                    _krapi_circuit.record_failure()
                logger.error("HTTP error %s for %s: %s", status, url, exc)
            except requests.RequestException as exc:
                logger.error("Request failed for %s: %s", url, exc)
            except ValueError as exc:
                logger.error("Failed to decode JSON from %s: %s", url, exc)
            return None
        return None

    def _post_json(self, url: str, payload: Optional[dict]) -> Optional[dict]:
        """Perform a POST request and return parsed JSON, or ``None`` on failure."""
        return self._request_json("POST", url, payload)

    def _get_json(self, url: str) -> Optional[dict]:
        """Perform a GET request and return parsed JSON, or ``None`` on failure."""
        return self._request_json("GET", url)

    # ------------------------------------------------------------------
    # kr-api data extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_search_results(data: dict | list) -> list:
        """Return the list of product dicts from a kr-api product-search response."""
        if isinstance(data, list):
            return data
        # ``result`` (singular) is the current ``v2/product-search`` shape;
        # the others are kept for older/alternative endpoints.
        for key in ("result", "results", "products", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    @staticmethod
    def _extract_total(data: dict) -> Optional[int]:
        """Return the total number of available items from a kr-api response."""
        for key in ("totalHits", "total", "totalCount", "count"):
            if isinstance(data.get(key), int):
                return data[key]
        return None

    @staticmethod
    def _parse_search_product(item: dict) -> Optional[Product]:
        """Parse a product from a kr-api ``/v2/product-search`` result item.

        The current API wraps each hit as ``{"id", "product": {...}, "score",
        ...}``; older shapes placed the product fields at the top level, so the
        ``product`` sub-object is unwrapped defensively.  Store-specific pricing
        is read from the ``mobilescan`` block when present.
        """
        if isinstance(item.get("product"), dict):
            item = item["product"]

        localized = item.get("localizedName") or {}
        name: str = (
            (localized.get("finnish") or localized.get("fi") or "")
            or item.get("name")
            or item.get("title")
            or item.get("productName")
            or ""
        ).strip()

        attrs = item.get("productAttributes") or {}
        ean: str = _normalize_ean(
            item.get("ean")
            or item.get("EAN")
            or item.get("barcode")
            or item.get("eanCode")
            or item.get("gtin")
            or attrs.get("ean")
            or ""
        )

        if not name and not ean:
            return None

        product_id = str(item.get("id") or item.get("productId") or "")

        images = item.get("images") or []
        attr_image = (attrs.get("image") or {}).get("url") or ""
        image_url = (
            item.get("imageUrl")
            or item.get("image")
            or (images[0] if images else "")
            or attr_image
        )

        description = (item.get("description") or "").strip()

        price, comp_price, comp_unit = _parse_mobilescan_pricing(item)

        return Product(
            name=name,
            ean=ean,
            product_id=product_id,
            description=description,
            image_url=image_url,
            price=price,
            comparison_price=comp_price,
            comparison_unit=comp_unit,
            extra=item,
        )

    @staticmethod
    def _parse_offer_product(offer: dict) -> Optional[Product]:
        """Parse a product from a kr-api ``/offer-category`` offer item."""
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
        ean: str = _normalize_ean(
            inner.get("ean")
            or attrs.get("ean")
            or inner.get("baseEan")
            or outer.get("ean")
            or ""
        )

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

        # Offer items carry their regular price in ``normalPricing`` (the
        # ``pricing`` block holds the campaign/discount price); prefer the
        # ``mobilescan`` block on the inner product when available.
        price, comp_price, comp_unit = _parse_mobilescan_pricing(inner)
        if price is None:
            normal_pricing = offer.get("normalPricing") or {}
            np_price = normal_pricing.get("price")
            if isinstance(np_price, (int, float)):
                price = float(np_price)
                np_unit = normal_pricing.get("unit") or {}
                comp_unit = np_unit.get("fi") or comp_unit

        return Product(
            name=name,
            ean=ean,
            product_id=product_id,
            description=description,
            image_url=image_url,
            price=price,
            comparison_price=comp_price,
            comparison_unit=comp_unit,
            extra=offer,
        )
