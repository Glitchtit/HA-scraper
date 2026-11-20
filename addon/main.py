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
import time
from dataclasses import replace
from pathlib import Path

from scraper.lookup import LookupFailed, resolve_ean
from scraper.storage_client import (
    StorageAPIError,
    StorageClient,
    create_in_storage,
    upload_image,
    write_availability,
)
from scraper.scraper import KRuokaScraper, Product

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
    upload_images: bool = True,
    price_scrapers: list[KRuokaScraper] | None = None,
    avail_scrapers: list[KRuokaScraper] | None = None,
    parent_id: int | None = None,
    product_group_id: int | None = None,
) -> int | None:
    """Create *product* in Storage with its price and per-store availability.

    Returns the new product ID, or ``None`` when its barcode already belongs
    to a product.  Raises ``StorageAPIError`` when it cannot be saved.
    When *price_scrapers* is provided, the product's K-Ruoka price is looked
    up by barcode and stored as ``unit_price``.
    """
    # Asked before the sweeps below: a product that will not be created
    # needs no store lookups.
    if product.ean and storage.get_product_by_barcode(product.ean):
        return None

    # Sweep per-store availability once; a kr-api sweep also carries the
    # price, which saves a second Cloudflare-bypassed query for the same EAN.
    availability = _collect_availability(product.ean, avail_scrapers or [])
    price = product.price
    if price is None:
        price = _price_from_entries(availability)
    if price is None and price_scrapers:
        price = _lookup_price(price_scrapers, product.ean)

    if not upload_images:
        product = replace(product, image_url="")
    return create_in_storage(
        storage, product, ean=product.ean, price=price,
        parent_id=parent_id, product_group_id=product_group_id,
        availability=availability,
    )


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

    try:
        product = resolve_ean(
            barcode, scrapers, searxng_url=getattr(args, "searxng_url", "")
        )
    except LookupFailed as exc:
        logger.warning("Lookup failed for EAN %s: %s.", barcode, exc)
        return {
            "success": False,
            "error": (
                f"Lookup failed for EAN {barcode}: {exc}. "
                "Nothing was saved; scan it again later."
            ),
        }

    if product is None:
        logger.info("EAN %s not found on K-Ruoka, S-kaupat, or SearXNG.", barcode)
        return {"success": False, "error": f"Product not found for EAN {barcode}"}

    logger.info("Found: '%s' (EAN %s).", product.name, product.ean)

    try:
        product_id = sync_product(
            product,
            storage,
            upload_images=args.upload_images,
            price_scrapers=price_scrapers,
            avail_scrapers=avail_scrapers,
        )
        already_existed = product_id is None
        if already_existed:
            # The barcode was added while it was being looked up.
            product_id = (storage.get_product_by_barcode(barcode) or {}).get("id")
    except StorageAPIError as exc:
        logger.error("Storage error for '%s': %s", product.name, exc)
        return {"success": False, "error": f"Found {product.name} but could not save it to Storage: {exc}"}
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
        "already_existed": already_existed,
    }


def _discover_products(args: argparse.Namespace) -> tuple[int, list[int]]:
    """Discover products via the Storage barcode queue.

    Fetches pending items from the barcode queue, searches online stores for
    each barcode, creates/stocks them in Storage, and marks queue items as done.

    For each barcode:

    1. Search K-Ruoka by EAN; fall back to S-kaupat, SearXNG.
    2. Create the product in Storage (via ``sync_product``).
    3. Mark the barcode queue item as done with the resulting product ID.
    4. Add 1 unit to Storage stock.

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

        try:
            product = resolve_ean(
                barcode, scrapers, searxng_url=getattr(args, "searxng_url", "")
            )
        except LookupFailed as exc:
            # Not "not found": the item stays pending for the next run.
            logger.warning(
                "  Lookup failed for EAN %s: %s – left in the queue.", barcode, exc
            )
            errors += 1
            continue

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

        try:
            product_id = sync_product(
                product,
                storage,
                upload_images=args.upload_images,
                price_scrapers=price_scrapers,
                avail_scrapers=avail_scrapers,
            )
            if product_id is None:
                # The barcode already belongs to a product: stock that one.
                product_id = (storage.get_product_by_barcode(barcode) or {}).get("id")
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
        if product_id is None:
            logger.error("  Could not find the product for EAN %s in Storage.", barcode)
            errors += 1
            continue

        # Record "done" before adding stock.  If it cannot be recorded the
        # item stays pending and the next run starts over, so stock is never
        # added twice for one queue item.
        if queue_id is not None:
            try:
                storage.update_barcode_queue_item(
                    queue_id, status="done", result_product_id=int(product_id),
                )
                logger.info("  → Marked queue item %s as done.", queue_id)
            except StorageAPIError as exc:
                logger.warning(
                    "  Could not mark queue item %s as done: %s – left in the queue.",
                    queue_id, exc,
                )
                errors += 1
                continue

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
    placeholder name, write a changed description, refresh the price and
    upload an image when the product has none.

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

    # Build product_id → list of EANs mapping.  Storage folds a multi-pack
    # into its single product and marks the pack's barcode with pack_size N:
    # single-item barcodes are tried first, and a pack's prices are divided
    # by N, so one can is not priced as the six-pack.
    pid_to_eans: dict[int, list[str]] = {}
    ean_pack: dict[str, float] = {}
    for entry in barcodes:
        pid = entry.get("product_id")
        ean = entry.get("barcode")
        if pid is not None and ean:
            pid_to_eans.setdefault(int(pid), []).append(str(ean))
            pack = entry.get("pack_size")
            ean_pack[str(ean)] = pack if isinstance(pack, (int, float)) and pack > 0 else 1
    for eans in pid_to_eans.values():
        eans.sort(key=lambda e: ean_pack[e] != 1)

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

        # Try each EAN until one is found.
        found: Product | None = None
        matched_ean = ""
        try:
            for ean in eans:
                found = resolve_ean(
                    ean, scrapers, searxng_url=getattr(args, "searxng_url", "")
                )
                if found is not None:
                    matched_ean = ean
                    break
        except LookupFailed as exc:
            # Nothing is known about the product this run: leave it as it
            # is, price refresh included.
            logger.warning(
                "  Product %d ('%s'): lookup failed (%s) – skipped.",
                pid, current_name, exc,
            )
            errors += 1
            continue

        # Refresh per-store availability regardless of whether the product
        # was found by name/EAN search above.  A store the lookup already
        # asked answers from that response (the scraper keeps it); the
        # entries are reused below for the price refresh.
        avail_ean = matched_ean or (eans[0] if eans else "")
        avail_entries: list[dict] = []
        if avail_ean:
            avail_entries = _collect_availability(avail_ean, avail_scrapers)
            if ean_pack.get(avail_ean, 1) != 1:
                for entry in avail_entries:
                    if entry.get("price") is not None:
                        entry["price"] = entry["price"] / ean_pack[avail_ean]
            write_availability(storage, pid, avail_entries)

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
        pack = ean_pack.get(matched_ean, 1)
        # A pack's description describes the pack, not the single product.
        if (pack == 1 and found.description
                and found.description != storage_product.get("description")):
            update_fields["description"] = found.description

        # Refresh the K-Ruoka price (kr-api only).  Reuse the price already
        # attached to *found* when it came from the kr-api backend, then the
        # availability sweep entries collected above, and only fall back to
        # a fresh _lookup_price query if neither yielded a price.
        # The availability entries are already per item.
        price = found.price / pack if found.price is not None else None
        if price is None:
            price = _price_from_entries(avail_entries)
        price_answered = True
        if price is None and price_scrapers and pid in price_refresh_ids:
            failed_before = [s.failed_requests for s in price_scrapers]
            price = _lookup_price(price_scrapers, matched_ean)
            if price is not None:
                price = price / pack
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

        # A product that has a picture keeps it.
        if found.image_url and args.upload_images and not storage_product.get("picture_filename"):
            upload_image(storage, pid, found.image_url, found.ean)

        updated += 1

    logger.info(
        "Update complete: updated: %d  skipped: %d  errors: %d",
        updated, skipped, errors,
    )
    return 0 if errors == 0 else 1
