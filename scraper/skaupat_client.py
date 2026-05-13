"""Lightweight client for looking up products on S-kaupat.fi by EAN.

S-kaupat.fi (formerly Foodie.fi) is the Finnish S-group online grocery
store.  Its Next.js front-end embeds full product data inside the
``__NEXT_DATA__`` JSON blob on product pages.  The URL scheme

    https://www.s-kaupat.fi/tuote/{ean}

accepts a bare EAN and 301-redirects to the canonical slug URL, making
it possible to look up any product without knowing its slug.  The
embedded Apollo state contains product name, EAN, description, brand,
and an image hosted on ``cdn.s-cloud.fi``.

No authentication is required.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.s-kaupat.fi"
_PRODUCT_PATH = "/tuote/{ean}"
_IMAGE_URL = "https://cdn.s-cloud.fi/v1/w720h720@_q75/product/ean/{ean}_kuva1.webp"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*(.*?)\s*</script>',
    re.DOTALL,
)


class SKaupatError(Exception):
    """Raised on unexpected HTTP or parsing errors."""


@dataclass
class SKaupatProduct:
    """Product data extracted from S-kaupat.fi."""

    name: str
    ean: str
    description: str = ""
    brand: str = ""
    image_url: str = ""


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    })
    return session


def lookup_ean(
    ean: str,
    *,
    session: requests.Session | None = None,
) -> Optional[SKaupatProduct]:
    """Look up a product on S-kaupat.fi by EAN code.

    Returns an :class:`SKaupatProduct` if found, or ``None`` if the
    product does not exist (HTTP 404).

    Raises :class:`SKaupatError` on unexpected HTTP or parsing failures.
    """
    sess = session or _make_session()
    url = _BASE_URL + _PRODUCT_PATH.format(ean=ean)
    logger.debug("S-kaupat: fetching %s", url)

    try:
        resp = sess.get(url, timeout=15, allow_redirects=True)
    except requests.RequestException as exc:
        raise SKaupatError(f"HTTP request failed: {exc}") from exc

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise SKaupatError(
            f"Unexpected HTTP {resp.status_code} for EAN {ean}"
        )

    # Extract __NEXT_DATA__ JSON blob.
    match = _NEXT_DATA_RE.search(resp.text)
    if not match:
        raise SKaupatError("Could not find __NEXT_DATA__ in response HTML")

    try:
        next_data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SKaupatError(f"Failed to parse __NEXT_DATA__: {exc}") from exc

    apollo: dict = (
        next_data
        .get("props", {})
        .get("pageProps", {})
        .get("apolloState", {})
    )

    # Find the Product entry whose EAN matches.
    for _key, val in apollo.items():
        if not isinstance(val, dict):
            continue
        if val.get("__typename") != "Product":
            continue
        if val.get("ean") != ean:
            continue

        name = val.get("name", "")
        if not name:
            continue

        image_url = _IMAGE_URL.format(ean=ean)
        return SKaupatProduct(
            name=name,
            ean=ean,
            description=val.get("description", ""),
            brand=val.get("brandName", ""),
            image_url=image_url,
        )

    # Product page loaded but we couldn't extract the product.
    logger.debug("S-kaupat: product page loaded but no matching Product in Apollo state")
    return None
