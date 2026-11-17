"""The scraper's operations: discover, update and the Storage writes.

Called by ``ingress_server.py``, which passes the add-on options as an
``argparse.Namespace`` (``store``, ``storage_url``, ``upload_images``,
``searxng_url``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import posixpath
import time
from pathlib import Path

from scraper.storage_client import StorageAPIError, StorageClient
from scraper.scraper import KRuokaScraper, Product
from scraper.searxng_client import SearXNGError, lookup_ean as searxng_lookup
from scraper.skaupat_client import SKaupatError, lookup_ean as skaupat_lookup

import requests

logger = logging.getLogger(__name__)


def _parse_store_ids(raw: str) -> list[str]:
    """Split a comma-separated store-ID string into a list of non-empty IDs."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _make_price_scrapers(store_ids: list[str]) -> list[KRuokaScraper]:
    """Build kr-api (price-capable) scrapers, one per store.

    Returns an empty list when FlareSolverr is not configured — callers then
    skip price lookups entirely: K-Ruoka prices are only exposed by the
    Cloudflare-protected kr-api backend.  Each scraper opens a CF-cleared
    session once and is reused across all EAN lookups.
    """
    if not os.environ.get("FLARESOLVERR_URL"):
        logger.info(
            "FlareSolverr is not configured; skipping K-Ruoka price lookups. "
            "Set flaresolverr_url in the add-on options to enable prices."
        )
        return []
    scrapers: list[KRuokaScraper] = []
    for sid in store_ids:
        try:
            scrapers.append(KRuokaScraper(store_id=sid, use_graphql=False))
        except Exception as exc:  # pragma: no cover - network/CF setup issues
            logger.warning("Could not initialise price scraper for store %s: %s", sid, exc)
    return scrapers


def _lookup_price(price_scrapers: list[KRuokaScraper], ean: str) -> float | None:
    """Return the K-Ruoka consumer unit price (EUR, VAT incl.) for *ean*.

    Tries each store's kr-api backend in order; returns the first price found,
    or ``None`` if the product is not stocked / has no price at any store.
    """
    if not ean:
        return None
    for scraper in price_scrapers:
        try:
            for p in scraper.search(ean, max_products=10):
                if p.ean == ean and p.price is not None:
                    return p.price
        except Exception as exc:
            logger.debug(
                "Price lookup failed for EAN %s at store %s: %s",
                ean, scraper.store_id, exc,
            )
    return None


# Store ids this process has already named in Storage (names do not change).
_registered_stores: set[str] = set()


def _register_stores(
    storage: StorageClient,
    store_ids: list[str],
    name_scrapers: list[KRuokaScraper],
) -> None:
    """Give every configured store its friendly name in Storage, once.

    Names come from kr-api ``store/{id}`` (first scraper that resolves wins).
    A store whose name cannot be resolved is left alone — Storage registers
    it under its raw ID on the first availability write and keeps any name
    it already has — and is tried again on the next operation.
    Best-effort: never raises.
    """
    for sid in store_ids:
        if sid in _registered_stores:
            continue
        name: str | None = None
        for scraper in name_scrapers:
            try:
                name = scraper.fetch_store_name(sid)
            except Exception as exc:
                logger.debug("Store-name lookup failed for %s: %s", sid, exc)
                name = None
            if name:
                break
        if not name:
            continue
        try:
            storage.upsert_store(sid, name)
            _registered_stores.add(sid)
        except StorageAPIError as exc:
            logger.warning("Could not register store %s in Storage: %s", sid, exc)


def _collect_availability(
    ean: str,
    avail_scrapers: list[KRuokaScraper],
) -> list[dict]:
    """Sweep all configured stores for *ean* and return the merged entries.

    Best-effort: scraper errors skip that scraper. A store that appears in
    several scrapers is included once (first result wins — kr-api scrapers
    are passed first so their price is preferred). Returns ``[]`` when *ean*
    or *avail_scrapers* is empty, or when no scraper yields a result.
    """
    if not ean or not avail_scrapers:
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    for scraper in avail_scrapers:
        try:
            results = scraper.check_store_availability(ean)
        except Exception as exc:
            logger.debug("Availability sweep failed on %s: %s",
                         getattr(scraper, "store_ids", "?"), exc)
            continue
        for a in results:
            if a.store_id in seen:
                continue
            seen.add(a.store_id)
            entry: dict = {"store_id": a.store_id, "available": a.available}
            if a.price is not None:
                entry["price"] = a.price
                entry["price_currency"] = a.price_currency
            entries.append(entry)
    return entries


def _write_availability(
    storage: StorageClient,
    product_id: int,
    entries: list[dict],
) -> None:
    """Upsert already-collected availability *entries* in Storage.

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


def _price_from_entries(entries: list[dict]) -> float | None:
    """Return the first available entry's price, or ``None``.

    Mirrors the "first store wins" ordering already used when building
    *entries* (kr-api scrapers are swept first), so this is equivalent to
    a fresh ``_lookup_price`` call but without re-querying anything.
    """
    for entry in entries:
        if entry.get("available") and entry.get("price") is not None:
            return entry["price"]
    return None


def sync_product(
    product: Product,
    storage: StorageClient,
    *,
    location_id: int | None,
    quantity_unit_id: int | None,
    skip_existing: bool,
    known_barcodes: set[str],
    upload_images: bool = True,
    price_scrapers: list[KRuokaScraper] | None = None,
    avail_scrapers: list[KRuokaScraper] | None = None,
) -> bool:
    """Add *product* to Storage.

    Returns ``True`` if the product was created/updated, ``False`` if skipped.
    Raises ``StorageAPIError`` if the product cannot be created or its barcode
    cannot be attached (the barcode-less product is then deleted again).
    When *price_scrapers* is provided, the product's K-Ruoka price is looked up
    by barcode and stored as ``unit_price`` on creation.
    """
    if not product.ean:
        logger.debug("Skipping '%s' – no EAN code.", product.name)
        return False

    if skip_existing and product.ean in known_barcodes:
        logger.debug("Skipping '%s' – EAN %s already in Storage.", product.name, product.ean)
        return False

    # Check live against the Storage API in case known_barcodes is stale.
    try:
        existing = storage.get_product_by_barcode(product.ean)
    except StorageAPIError as exc:
        logger.warning("Could not check barcode %s in Storage: %s", product.ean, exc)
        existing = None

    if existing and skip_existing:
        logger.debug(
            "Skipping '%s' – EAN %s already mapped to product %s.",
            product.name,
            product.ean,
            existing.get("id") or existing.get("name"),
        )
        known_barcodes.add(product.ean)
        return False

    # Sweep per-store availability once up front so its entries can also
    # supply the creation price below — avoids a second Cloudflare-bypassed
    # kr-api query for the same EAN.
    avail_entries: list[dict] = []
    if avail_scrapers:
        avail_entries = _collect_availability(product.ean, avail_scrapers)

    # Resolve a K-Ruoka price for this barcode (kr-api only; needs CF bypass).
    price = product.price
    if price is None:
        price = _price_from_entries(avail_entries)
    if price is None and price_scrapers:
        price = _lookup_price(price_scrapers, product.ean)
    price_kwargs: dict = {}
    if price is not None:
        price_kwargs["unit_price"] = price
        price_kwargs["unit_price_currency"] = "EUR"

    # Create the product entry (a StorageAPIError propagates to the caller).
    product_id = storage.create_product(
        name=product.name,
        description=product.description,
        location_id=location_id,
        unit_id=quantity_unit_id,
        **price_kwargs,
    )
    if price is not None:
        logger.info(
            "Created product '%s' (Storage product ID %d, price %.2f EUR).",
            product.name, product_id, price,
        )
    else:
        logger.info("Created product '%s' (Storage product ID %d).", product.name, product_id)

    # Attach the EAN barcode.  A product without it is never found by barcode,
    # so every later scan would create another one: delete it and fail instead.
    try:
        storage.add_barcode(product_id, product.ean)
        known_barcodes.add(product.ean)
        logger.info("  → Added barcode %s.", product.ean)
    except StorageAPIError as exc:
        logger.error(
            "Product '%s' created but failed to add barcode %s: %s",
            product.name,
            product.ean,
            exc,
        )
        try:
            storage.delete_product(product_id)
            logger.info("  → Deleted product %d again.", product_id)
        except StorageAPIError as del_exc:
            logger.error("Could not delete barcode-less product %d: %s", product_id, del_exc)
        raise

    # Upload product image.
    if upload_images and product.image_url:
        _upload_product_image(product, storage, product_id)

    # Record per-store assortment availability (best-effort) using the
    # entries already collected above — no second sweep.
    if avail_scrapers:
        _write_availability(storage, product_id, avail_entries)

    return True


def _upload_product_image(product: Product, storage: StorageClient, product_id: int) -> None:
    """Download the product image and upload it to Storage."""
    url = product.image_url
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Could not download image for '%s': %s", product.name, exc)
        return

    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    ext = _image_extension(content_type, url)
    filename = f"{product.ean}{ext}"

    try:
        storage.upload_product_image(
            product_id, filename, resp.content, content_type=content_type
        )
        logger.info("  → Uploaded image %s.", filename)
    except StorageAPIError as exc:
        logger.warning("Could not upload image for '%s': %s", product.name, exc)


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


def _discover_single_barcode(
    args: argparse.Namespace,
    barcode: str,
) -> dict:
    """Discover a single barcode by searching K-Ruoka / S-kaupat and syncing to Storage.

    The caller already knows the barcode.  It searches online stores, creates
    the product in Storage, adds 1 unit to stock, and returns a result dict.

    Returns ``{"success": True, "product": {...}, "product_id": int}`` on
    success, or ``{"success": False, "error": "..."}`` on failure.
    """
    storage = StorageClient(base_url=args.storage_url)
    store_ids = _parse_store_ids(args.store)
    scrapers = [KRuokaScraper(store_id=sid) for sid in store_ids]
    price_scrapers = _make_price_scrapers(store_ids)
    # Availability is answerable by the un-throttled GraphQL backend;
    # reserve the rate-limited kr-api strictly for price lookups.
    avail_scrapers: list[KRuokaScraper] = scrapers
    _register_stores(storage, store_ids, price_scrapers)

    # Check if barcode already exists in Storage.
    try:
        existing = storage.get_product_by_barcode(barcode)
        if existing:
            name = existing.get("name", barcode)
            product_id = existing.get("id")
            logger.info("Barcode %s already in Storage as '%s' (ID %s).", barcode, name, product_id)
            return {
                "success": True,
                "product": {"name": name, "barcode": barcode},
                "product_id": product_id,
                "already_existed": True,
            }
    except StorageAPIError as exc:
        logger.debug("Barcode lookup failed (proceeding): %s", exc)

    logger.info("Looking up EAN %s …", barcode)

    # Search K-Ruoka by EAN across configured stores.
    product = None
    for scraper in scrapers:
        try:
            for p in scraper.search(barcode, max_products=10):
                if p.ean == barcode:
                    product = p
                    break
        except Exception as exc:
            logger.warning(
                "Store %s failed for EAN %s (%s); trying next store …",
                scraper.store_id, barcode, exc,
            )
        if product is not None:
            break

    # Fallback: S-kaupat EAN lookup.
    if product is None:
        try:
            sk = skaupat_lookup(barcode)
            if sk is not None:
                logger.info("Found on S-kaupat: '%s'.", sk.name)
                product = Product(
                    name=sk.name,
                    ean=sk.ean,
                    description=sk.description,
                    image_url=sk.image_url,
                )
        except SKaupatError as exc:
            logger.debug("S-kaupat lookup failed: %s", exc)

    # Fallback: SearXNG web search.
    if product is None and getattr(args, "searxng_url", ""):
        try:
            sx = searxng_lookup(barcode, searxng_url=args.searxng_url)
            if sx is not None:
                logger.info("Found via SearXNG: '%s'.", sx.name)
                product = Product(
                    name=sx.name,
                    ean=sx.ean,
                    image_url=sx.image_url,
                )
        except SearXNGError as exc:
            logger.debug("SearXNG lookup failed: %s", exc)

    if product is None:
        logger.info("EAN %s not found on K-Ruoka, S-kaupat, or SearXNG.", barcode)
        return {"success": False, "error": f"Product not found for EAN {barcode}"}

    logger.info("Found: '%s' (EAN %s).", product.name, product.ean)

    # Sync product to Storage.
    known_barcodes: set[str] = set()
    try:
        added = sync_product(
            product,
            storage,
            location_id=args.location_id,
            quantity_unit_id=args.quantity_unit_id,
            skip_existing=False,
            known_barcodes=known_barcodes,
            upload_images=args.upload_images,
            price_scrapers=price_scrapers,
            avail_scrapers=avail_scrapers,
        )
    except StorageAPIError as exc:
        logger.error("Storage error for '%s': %s", product.name, exc)
        return {"success": False, "error": f"Found {product.name} but could not save it to Storage: {exc}"}

    # Look up the Storage product ID.
    product_id = None
    try:
        existing = storage.get_product_by_barcode(barcode)
        if existing:
            product_id = existing.get("id")
    except StorageAPIError as exc:
        logger.error("Could not look up barcode %s after saving: %s", barcode, exc)
    if product_id is None:
        return {
            "success": False,
            "error": f"Found {product.name} but could not save it to Storage (see scraper logs)",
        }

    # Add 1 unit to stock.
    try:
        storage.add_stock(int(product_id), amount=1.0)
        logger.info("Added 1 unit to Storage stock (product ID %s).", product_id)
    except (StorageAPIError, ValueError) as exc:
        logger.warning("Could not add stock for '%s': %s", product.name, exc)

    logger.info("Single-barcode discover complete for EAN %s.", barcode)
    return {
        "success": True,
        "product": {
            "name": product.name,
            "barcode": product.ean,
            "description": product.description or "",
        },
        "product_id": product_id,
        "already_existed": not added,
    }


def _discover_products(args: argparse.Namespace) -> tuple[int, list[int]]:
    """Discover products via the Storage barcode queue.

    Fetches pending items from the barcode queue, searches online stores for
    each barcode, creates/stocks them in Storage, and marks queue items as done.

    For each barcode:

    1. Search K-Ruoka by EAN; fall back to S-kaupat, SearXNG.
    2. Create the product in Storage (via ``sync_product``).
    3. Add 1 unit to Storage stock.
    4. Mark the barcode queue item as done with the resulting product ID.

    Returns a ``(return_code, product_ids)`` tuple.  *return_code* is 0 on
    success and 1 if any errors occurred.  *product_ids* contains the Storage
    IDs of all products that were successfully created or stocked during this
    run.
    """
    storage = StorageClient(base_url=args.storage_url)
    store_ids = _parse_store_ids(args.store)
    scrapers = [KRuokaScraper(store_id=sid) for sid in store_ids]
    price_scrapers = _make_price_scrapers(store_ids)
    # Availability is answerable by the un-throttled GraphQL backend;
    # reserve the rate-limited kr-api strictly for price lookups.
    avail_scrapers: list[KRuokaScraper] = scrapers
    _register_stores(storage, store_ids, price_scrapers)

    # Pre-load known barcodes.
    known_barcodes: set[str] = set()
    try:
        for entry in storage.get_all_barcodes():
            bc = entry.get("barcode")
            if bc:
                known_barcodes.add(str(bc))
        logger.info("  %d barcode(s) already registered in Storage.", len(known_barcodes))
    except StorageAPIError as exc:
        logger.warning("Could not fetch existing barcodes: %s", exc)

    # Fetch pending barcodes from the Storage barcode queue.
    try:
        pending = storage.get_barcode_queue(status="pending")
    except StorageAPIError as exc:
        logger.error("Failed to fetch barcode queue: %s", exc)
        return 1, []

    if not pending:
        logger.info("No pending barcodes in the queue.")
        return 0, []

    logger.info("Found %d pending barcode(s) in the queue.", len(pending))

    created = skipped = errors = 0
    discovered_ids: list[int] = []

    for entry in pending:
        barcode = entry.get("barcode", "")
        queue_id = entry.get("id")
        logger.info("Looking up EAN %s …", barcode)

        # Search K-Ruoka first; fall back to S-kaupat, then SearXNG.
        product = None
        for scraper in scrapers:
            try:
                for p in scraper.search(barcode, max_products=10):
                    if p.ean == barcode:
                        product = p
                        break
            except Exception as exc:
                logger.warning(
                    "  Store %s failed for EAN %s (%s); trying next store …",
                    scraper.store_id, barcode, exc,
                )
            if product is not None:
                break

        # Fallback: try S-kaupat.fi product lookup by EAN.
        if product is None:
            try:
                sk = skaupat_lookup(barcode)
                if sk is not None:
                    logger.info("  Found on S-kaupat: '%s'.", sk.name)
                    product = Product(
                        name=sk.name,
                        ean=sk.ean,
                        description=sk.description,
                        image_url=sk.image_url,
                    )
            except SKaupatError as exc:
                logger.debug("  S-kaupat lookup failed: %s", exc)

        # Fallback: SearXNG web search.
        if product is None and getattr(args, "searxng_url", ""):
            try:
                sx = searxng_lookup(barcode, searxng_url=args.searxng_url)
                if sx is not None:
                    logger.info("  Found via SearXNG: '%s'.", sx.name)
                    product = Product(
                        name=sx.name,
                        ean=sx.ean,
                        image_url=sx.image_url,
                    )
            except SearXNGError as exc:
                logger.debug("  SearXNG lookup failed: %s", exc)

        if product is None:
            logger.info("  EAN %s not found on K-Ruoka, S-kaupat, or SearXNG – skipping.", barcode)
            if queue_id is not None:
                try:
                    storage.update_barcode_queue_item(
                        queue_id, status="error",
                        error_message=f"Product not found for EAN {barcode}",
                    )
                except StorageAPIError:
                    pass
            skipped += 1
            continue

        logger.info("  Found: '%s' (EAN %s).", product.name, product.ean)

        # Sync product to Storage.
        try:
            added = sync_product(
                product,
                storage,
                location_id=args.location_id,
                quantity_unit_id=args.quantity_unit_id,
                # An already-mapped barcode is stocked below, not re-created.
                skip_existing=True,
                known_barcodes=known_barcodes,
                upload_images=args.upload_images,
                avail_scrapers=avail_scrapers,
            )
        except StorageAPIError as exc:
            logger.error("Storage error for '%s': %s", product.name, exc)
            if queue_id is not None:
                try:
                    storage.update_barcode_queue_item(
                        queue_id, status="error", error_message=str(exc),
                    )
                except StorageAPIError:
                    pass
            errors += 1
            continue

        if not added:
            # Product already exists — look up its Storage product ID for stocking.
            existing = storage.get_product_by_barcode(barcode)
            if existing:
                product_id = existing.get("id")
            else:
                logger.info("  Product already in Storage – skipping.")
                skipped += 1
                continue
        else:
            # Newly created — get the Storage product ID via barcode lookup.
            existing = storage.get_product_by_barcode(barcode)
            product_id = existing.get("id") if existing else None

        # Add to Storage stock.
        if product_id is not None:
            discovered_ids.append(int(product_id))
            raw_stock = entry.get("import_stock_amount")
            is_storage_import = entry.get("source") == "storage-import"
            if is_storage_import and raw_stock is None:
                # Storage import with NULL amount = product had no stock in Storage — skip.
                logger.info(
                    "  → Skipping stock for product ID %s (not in Storage stock).",
                    product_id,
                )
            else:
                stock_amount = float(raw_stock) if raw_stock is not None else 1.0
                try:
                    storage.add_stock(int(product_id), amount=stock_amount)
                    logger.info(
                        "  → Added %g unit(s) to Storage stock (product ID %s).",
                        stock_amount, product_id,
                    )
                except (StorageAPIError, ValueError) as exc:
                    logger.warning("  Could not add stock for '%s': %s", product.name, exc)

        # Mark the queue item as done.
        if queue_id is not None:
            try:
                storage.update_barcode_queue_item(
                    queue_id, status="done",
                    result_product_id=int(product_id) if product_id else None,
                )
                logger.info("  → Marked queue item %s as done.", queue_id)
            except StorageAPIError as exc:
                logger.warning(
                    "  Could not update queue item %s: %s", queue_id, exc,
                )

        created += 1

    logger.info(
        "Discover complete: created/stocked: %d  not found: %d  errors: %d",
        created, skipped, errors,
    )
    return (0 if errors == 0 else 1), discovered_ids


# --------------------------------------------------------------------------
# Price refresh budget — kr-api is rate limited, so Update refreshes at most
# _PRICE_BUDGET products per run, oldest-refreshed first, skipping products
# refreshed within _PRICE_TTL_SECONDS.  State lives in /data so rotation
# survives restarts.
# --------------------------------------------------------------------------

_PRICE_BUDGET = 200
_PRICE_TTL_SECONDS = 3 * 86400


def _price_state_path() -> Path:
    return Path("/data/price_refresh.json")


def _load_price_state() -> dict[str, float]:
    """product_id (str) → unix time of last kr-api price refresh."""
    try:
        data = json.loads(_price_state_path().read_text())
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_price_state(state: dict[str, float]) -> None:
    """Best-effort persist — never raises."""
    path = _price_state_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        # Written after every refreshed product, so replace atomically: a run
        # killed mid-write must not leave a truncated file.
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
    except OSError:
        pass


def _select_price_refresh_ids(
    products: list[dict], state: dict[str, float], now: float,
) -> tuple[set[int], int]:
    """Pick up to the budget of stale products, oldest-refreshed first.

    Returns ``(selected_ids, deferred_count)`` where *deferred_count* is the
    number of stale products that didn't fit in this run's budget.
    """
    stale = [
        p for p in products
        if now - state.get(str(p["id"]), 0.0) >= _PRICE_TTL_SECONDS
    ]
    stale.sort(key=lambda p: state.get(str(p["id"]), 0.0))
    selected = {int(p["id"]) for p in stale[:_PRICE_BUDGET]}
    return selected, max(0, len(stale) - len(selected))


def _is_placeholder_name(name: str) -> bool:
    """True for names that carry no real product information.

    Covers empty names, the SearXNG Strategy-3 fallback ``Unknown product
    (<EAN>)``, and names that are just a bare barcode.
    """
    stripped = name.strip()
    return (
        not stripped
        or stripped.startswith("Unknown product (")
        or stripped.isdigit()
    )


def _update_products(args: argparse.Namespace) -> int:
    """Update existing Storage products with names and images from K-Ruoka / S-kaupat.

    For each product in Storage that has at least one barcode, search K-Ruoka
    by EAN.  If not found, try S-kaupat.  When a match is found, fill in a
    placeholder name, update the description and upload the product image.

    Returns 0 on success, 1 if any errors occurred.
    """
    storage = StorageClient(base_url=args.storage_url)
    store_ids = _parse_store_ids(args.store)
    scrapers = [KRuokaScraper(store_id=sid) for sid in store_ids]
    # Prices come only from the Cloudflare-protected kr-api backend, so they are
    # fetched via a separate set of kr-api scrapers (independent of the GraphQL
    # name/image lookups above).  Empty when no CF bypass is configured.
    price_scrapers = _make_price_scrapers(store_ids)
    # Availability is answerable by the un-throttled GraphQL backend;
    # reserve the rate-limited kr-api strictly for price lookups.
    avail_scrapers: list[KRuokaScraper] = scrapers
    _register_stores(storage, store_ids, price_scrapers)

    try:
        products = storage.get_all_products()
    except StorageAPIError as exc:
        logger.error("Failed to fetch products from Storage: %s", exc)
        return 1

    try:
        barcodes = storage.get_all_barcodes()
    except StorageAPIError as exc:
        logger.error("Failed to fetch barcodes from Storage: %s", exc)
        return 1

    # Build product_id → list of EANs mapping.
    pid_to_eans: dict[int, list[str]] = {}
    for entry in barcodes:
        pid = entry.get("product_id")
        ean = entry.get("barcode")
        if pid is not None and ean:
            pid_to_eans.setdefault(int(pid), []).append(str(ean))

    if not products:
        logger.info("No products in Storage – nothing to update.")
        return 0

    logger.info("Updating %d product(s) from K-Ruoka / S-kaupat …", len(products))
    updated = skipped = errors = 0

    # kr-api price lookups are budgeted per run (rate-limit avoidance).
    price_state = _load_price_state()
    now = time.time()
    price_refresh_ids: set[int] = set()
    if price_scrapers:
        # Only a product with a barcode can be priced; the others must not
        # take budget slots.
        price_refresh_ids, deferred = _select_price_refresh_ids(
            [p for p in products if pid_to_eans.get(int(p["id"]))],
            price_state, now,
        )
        if deferred:
            logger.info(
                "Price refresh budget (%d) reached — %d stale product(s) "
                "deferred to a later run.", _PRICE_BUDGET, deferred,
            )

    for storage_product in products:
        pid = int(storage_product["id"])
        current_name = storage_product.get("name", "?")
        eans = pid_to_eans.get(pid, [])

        if not eans:
            logger.debug("  Product %d ('%s') has no barcodes – skipping.", pid, current_name)
            skipped += 1
            continue

        # Try each EAN until we find a match, trying all configured stores.
        found: Product | None = None
        matched_ean = ""
        for ean in eans:
            # K-Ruoka first — try each store.
            for scraper in scrapers:
                try:
                    for p in scraper.search(ean, max_products=10):
                        if p.ean == ean:
                            found = p
                            matched_ean = ean
                            break
                except Exception as exc:
                    logger.warning(
                        "  Store %s failed for EAN %s (%s); trying next store …",
                        scraper.store_id, ean, exc,
                    )
                if found:
                    break
            if found:
                break

            # S-kaupat fallback.
            try:
                sk = skaupat_lookup(ean)
                if sk is not None:
                    found = Product(
                        name=sk.name,
                        ean=sk.ean,
                        description=sk.description,
                        image_url=sk.image_url,
                    )
                    matched_ean = ean
                    break
            except SKaupatError as exc:
                logger.debug("  S-kaupat lookup failed for %s: %s", ean, exc)

            # SearXNG fallback.
            if getattr(args, "searxng_url", ""):
                try:
                    sx = searxng_lookup(ean, searxng_url=args.searxng_url)
                    if sx is not None:
                        found = Product(
                            name=sx.name,
                            ean=sx.ean,
                            image_url=sx.image_url,
                        )
                        matched_ean = ean
                        break
                except SearXNGError as exc:
                    logger.debug("  SearXNG lookup failed for %s: %s", ean, exc)

        # Refresh per-store availability regardless of whether the product
        # was found by name/EAN search above.  Collect once and reuse the
        # entries below for the price refresh — avoids a second
        # Cloudflare-bypassed kr-api query for the same EAN.
        avail_ean = matched_ean or (eans[0] if eans else "")
        avail_entries: list[dict] = []
        if avail_ean:
            avail_entries = _collect_availability(avail_ean, avail_scrapers)
            _write_availability(storage, pid, avail_entries)

        if found is None:
            # Nothing to price: stamp it anyway, so unfound products rotate
            # out instead of re-selecting every run.
            if pid in price_refresh_ids:
                price_state[str(pid)] = now
                _save_price_state(price_state)
            logger.debug("  Product %d ('%s') not found online – skipping.", pid, current_name)
            skipped += 1
            continue

        # Update product in Storage.
        update_fields: dict = {}
        # Only a placeholder name is replaced: a real one may be a rename made
        # in Storage, by hand or by its optimizer, and must not be undone.
        if found.name and found.name != current_name and _is_placeholder_name(current_name):
            update_fields["name"] = found.name
        if found.description:
            update_fields["description"] = found.description

        # Refresh the K-Ruoka price (kr-api only).  Reuse the price already
        # attached to *found* when it came from the kr-api backend, then the
        # availability sweep entries collected above, and only fall back to
        # a fresh _lookup_price query if neither yielded a price.
        price = found.price
        if price is None:
            price = _price_from_entries(avail_entries)
        price_answered = True
        if price is None and price_scrapers and pid in price_refresh_ids:
            failed_before = [s.failed_requests for s in price_scrapers]
            price = _lookup_price(price_scrapers, matched_ean)
            # No price and a failed request (rate limit, 403, breaker open)
            # is no answer: leave the product for the next run.
            price_answered = price is not None or failed_before == [
                s.failed_requests for s in price_scrapers
            ]
        if price is not None and storage_product.get("unit_price") != price:
            update_fields["unit_price"] = price
            update_fields["unit_price_currency"] = "EUR"

        if update_fields:
            try:
                storage.update_product(pid, **update_fields)
                new_name = update_fields.get("name", current_name)
                price_note = (
                    f" (price {update_fields['unit_price']:.2f} EUR)"
                    if "unit_price" in update_fields else ""
                )
                logger.info(
                    "  Updated product %d: '%s' → '%s'%s.",
                    pid, current_name, new_name, price_note,
                )
            except StorageAPIError as exc:
                logger.error("  Failed to update product %d ('%s'): %s", pid, current_name, exc)
                errors += 1
                continue
        else:
            logger.debug("  Product %d ('%s') already up to date.", pid, current_name)

        # kr-api answered and Storage took the result: the price is fresh.
        # Saved per product, so a run that dies later does not spend the
        # same budget again.
        if pid in price_refresh_ids and price_answered:
            price_state[str(pid)] = now
            _save_price_state(price_state)

        # Upload image if available.
        if found.image_url and args.upload_images:
            _upload_product_image(found, storage, pid)

        updated += 1

    logger.info(
        "Update complete: updated: %d  skipped: %d  errors: %d",
        updated, skipped, errors,
    )
    return 0 if errors == 0 else 1
