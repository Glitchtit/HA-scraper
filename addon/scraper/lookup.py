"""Look a product up by barcode across every source the scraper knows."""

from __future__ import annotations

import logging
from typing import Optional

from .scraper import KRuokaScraper, Product
from .searxng_client import SearXNGError, lookup_ean as searxng_lookup
from .skaupat_client import SKaupatError, lookup_ean as skaupat_lookup

logger = logging.getLogger(__name__)


class LookupFailed(Exception):
    """K-Ruoka could not be asked and no other source found the product.

    Unlike a ``None`` result this says nothing about whether the product
    exists: the caller should try again later rather than record "not found".
    """


def resolve_ean(
    ean: str,
    scrapers: list[KRuokaScraper],
    *,
    searxng_url: Optional[str] = None,
) -> Optional[Product]:
    """Return the product behind *ean*, or ``None`` when no source has it.

    Sources in order: K-Ruoka (each store's scraper in turn), S-kaupat, then
    SearXNG when *searxng_url* is set.  The first hit wins.

    K-Ruoka is the catalogue, the other two are fallbacks, so only its
    failure is a failed lookup: when a store's scraper got no answer and
    S-kaupat does not have the product either, :class:`LookupFailed` is
    raised, and SearXNG is not asked — a web-search name must not stand in
    for the catalogue during an outage.  A fallback that fails while
    K-Ruoka answered is logged and counts as "not there".
    """
    unanswered: list[str] = []
    for scraper in scrapers:
        failed_before = scraper.failed_requests
        try:
            for product in scraper.search(ean, max_products=10):
                if product.ean == ean:
                    return product
        except Exception as exc:
            logger.warning(
                "K-Ruoka store %s failed for EAN %s: %s", scraper.store_id, ean, exc
            )
            unanswered.append(scraper.store_id)
            continue
        if scraper.failed_requests != failed_before:
            unanswered.append(scraper.store_id)

    try:
        found = skaupat_lookup(ean)
    except SKaupatError as exc:
        logger.warning("S-kaupat lookup failed for EAN %s: %s", ean, exc)
    else:
        if found is not None:
            logger.info("Found on S-kaupat: '%s'.", found.name)
            return Product(
                name=found.name,
                ean=found.ean,
                description=found.description,
                image_url=found.image_url,
            )

    if unanswered:
        raise LookupFailed(
            f"K-Ruoka did not answer (store {', '.join(unanswered)})"
        )

    if searxng_url:
        try:
            found = searxng_lookup(ean, searxng_url=searxng_url)
        except SearXNGError as exc:
            logger.warning("SearXNG lookup failed for EAN %s: %s", ean, exc)
        else:
            if found is not None:
                logger.info("Found via SearXNG: '%s'.", found.name)
                return Product(
                    name=found.name, ean=found.ean, image_url=found.image_url
                )

    return None
