"""Lightweight client for looking up products via a SearXNG instance.

SearXNG (https://docs.searxng.org/) is a self-hosted meta-search engine.
This client searches for a product EAN and tries to extract product info
from the results — in particular from k-ruoka.fi product page URLs which
embed the product name in the slug::

    https://www.k-ruoka.fi/kauppa/tuote/capsi-merisuolamylly-230g-barcelona-6430025642017

The Kesko image CDN at ``public.keskofiles.com`` serves product images
without authentication or store context.

Requires SearXNG to have JSON format enabled
(``search.formats: [html, json]`` in ``settings.yml``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

_KESKO_IMAGE_CDN = "https://public.keskofiles.com/f/k-ruoka/product/{ean}?w=400"

# Matches k-ruoka.fi product page URLs and extracts the slug + EAN.
_KRUOKA_URL_RE = re.compile(
    r"https?://(?:www\.)?k-ruoka\.fi/kauppa/tuote/(?P<slug>[a-z0-9-]+)-(?P<ean>\d{8,14})(?:\?|#|$)",
    re.IGNORECASE,
)

_REQUEST_TIMEOUT = 15  # seconds


class SearXNGError(Exception):
    """Raised on unexpected HTTP or parsing errors."""


@dataclass
class SearXNGProduct:
    """Product data extracted from SearXNG search results."""

    name: str
    ean: str
    image_url: str = ""
    source_url: str = ""


def _slug_to_name(slug: str) -> str:
    """Convert a URL slug to a human-readable product name.

    ``capsi-merisuolamylly-230g-barcelona`` → ``Capsi merisuolamylly 230g barcelona``
    """
    return slug.replace("-", " ").strip().capitalize()


def _check_kesko_cdn(ean: str, session: requests.Session) -> str | None:
    """Return the Kesko CDN image URL if the product exists, else None."""
    url = _KESKO_IMAGE_CDN.format(ean=ean)
    try:
        resp = session.head(url, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        content_type = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "image" in content_type:
            return url
    except requests.RequestException as exc:
        logger.debug("Kesko CDN check failed for %s: %s", ean, exc)
    return None


def lookup_ean(
    ean: str,
    *,
    searxng_url: str,
    session: requests.Session | None = None,
) -> Optional[SearXNGProduct]:
    """Search SearXNG for a product by EAN.

    Queries SearXNG's JSON API for the EAN, then:
    1. Scans results for k-ruoka.fi product URLs (extracts name from slug)
    2. Falls back to the first result's title as product name
    3. Checks Kesko image CDN for a product image

    Returns a :class:`SearXNGProduct` if a product name could be determined,
    or ``None`` if no useful results were found.

    Raises :class:`SearXNGError` on HTTP or parsing failures.
    """
    sess = session or requests.Session()

    base = searxng_url.rstrip("/")
    params = urlencode({"q": ean, "format": "json"})
    url = f"{base}/search?{params}"

    try:
        resp = sess.get(url, timeout=_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise SearXNGError(f"SearXNG request failed: {exc}") from exc

    if resp.status_code != 200:
        raise SearXNGError(
            f"SearXNG returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise SearXNGError(f"SearXNG returned invalid JSON: {exc}") from exc

    results = data.get("results", [])
    if not results:
        return None

    # Strategy 1: find a k-ruoka.fi product URL (best source — name in slug).
    for result in results:
        result_url = result.get("url", "")
        m = _KRUOKA_URL_RE.search(result_url)
        if m and m.group("ean") == ean:
            name = _slug_to_name(m.group("slug"))
            image_url = _check_kesko_cdn(ean, sess) or ""
            logger.info("SearXNG: found k-ruoka URL for EAN %s: '%s'", ean, name)
            return SearXNGProduct(
                name=name,
                ean=ean,
                image_url=image_url,
                source_url=result_url,
            )

    # Strategy 2: use the first result's title as the product name,
    # but only if the EAN appears somewhere in the result (URL or content)
    # to avoid false positives.
    for result in results:
        result_url = result.get("url", "")
        content = result.get("content", "")
        title = result.get("title", "").strip()
        if ean in result_url or ean in content:
            if title:
                image_url = _check_kesko_cdn(ean, sess) or ""
                logger.info(
                    "SearXNG: using result title for EAN %s: '%s'",
                    ean, title,
                )
                return SearXNGProduct(
                    name=title,
                    ean=ean,
                    image_url=image_url,
                    source_url=result_url,
                )

    # Strategy 3: EAN found in results but no good name — check Kesko CDN
    # to at least confirm the product exists (caller can combine with BB name).
    image_url = _check_kesko_cdn(ean, sess)
    if image_url:
        logger.info(
            "SearXNG: no name found but Kesko CDN has image for EAN %s", ean
        )
        return SearXNGProduct(
            name=f"Unknown product ({ean})",
            ean=ean,
            image_url=image_url,
        )

    return None
