"""Main entry point for the grocy_scraper CLI.

Usage examples
--------------
Search for a specific product (GraphQL backend, no setup needed)::

    python main.py --store N110 --query "maito" \\
        --grocy-url https://grocy.example.com --grocy-key MY_API_KEY

Browse the full catalogue (GraphQL backend, no setup needed)::

    python main.py --store N110 --browse \\
        --grocy-url https://grocy.example.com --grocy-key MY_API_KEY

Multiple stores with automatic fallback::

    python main.py --store N110,N137 --query "maito" --dry-run

Dry-run (scrape only, do not write to Grocy)::

    python main.py --store N110 --query "maito" --dry-run

Force the kr-api fallback backend (requires Cloudflare bypass)::

    python main.py --store N110 --query "maito" --no-graphql --dry-run

Configuration can also be provided via environment variables or a ``.env``
file (see ``.env.example``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import posixpath
import re
import sys
import time

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; fall back to plain env vars

from grocy_scraper.barcodebuddy_client import BarcodeBuddyClient, BarcodeBuddyError
from grocy_scraper.grocy_client import GrocyAPIError, GrocyClient
from grocy_scraper.scraper import KRuokaScraper, Product
from grocy_scraper.searxng_client import SearXNGError, lookup_ean as searxng_lookup
from grocy_scraper.skaupat_client import SKaupatError, lookup_ean as skaupat_lookup

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _env_int(name: str) -> int | None:
    """Return an env-var value as *int*, or ``None`` if unset/empty."""
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    return int(val)


def _parse_store_ids(raw: str) -> list[str]:
    """Split a comma-separated store-ID string into a list of non-empty IDs."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape k-ruoka.fi for Finnish food products and populate a Grocy database."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Scraping options (not required when --sort / --date are used)
    scrape_group = parser.add_mutually_exclusive_group(required=False)
    scrape_group.add_argument(
        "--query",
        metavar="TERM",
        help="Search for products matching this term.",
    )
    scrape_group.add_argument(
        "--browse",
        action="store_true",
        default=False,
        help="Browse the full product catalogue (may be very large).",
    )
    scrape_group.add_argument(
        "--discover",
        action="store_true",
        default=False,
        help=(
            "Fetch unknown barcodes from Barcode Buddy, search K-Ruoka for "
            "matching products, add them to Grocy, stock them, and remove "
            "from the Barcode Buddy unknown list.  "
            "Requires --bbuddy-url and --bbuddy-user/--bbuddy-password (or env vars)."
        ),
    )
    scrape_group.add_argument(
        "--delete-all",
        action="store_true",
        default=False,
        help=(
            "Delete ALL products from the Grocy database.  "
            "This is a destructive operation and cannot be undone."
        ),
    )
    scrape_group.add_argument(
        "--update",
        action="store_true",
        default=False,
        help=(
            "Update all existing Grocy products with names and images from "
            "K-Ruoka (or S-kaupat as fallback).  Products are matched by "
            "their barcode.  Requires --store."
        ),
    )

    parser.add_argument(
        "--store",
        default=os.environ.get("KRUOKA_STORE_ID", ""),
        metavar="STORE_IDS",
        help=(
            "Comma-separated K-group store IDs (e.g. N110,N137).  "
            "If a scrape fails for the first store, the next store is tried "
            "automatically.  "
            "Also read from the KRUOKA_STORE_ID environment variable."
        ),
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        metavar="N",
        help="Stop after scraping N products.",
    )

    # Grocy options
    parser.add_argument(
        "--grocy-url",
        default=os.environ.get("GROCY_BASE_URL", ""),
        metavar="URL",
        help=(
            "Base URL of the Grocy instance (e.g. https://grocy.example.com).  "
            "Also read from the GROCY_BASE_URL environment variable."
        ),
    )
    parser.add_argument(
        "--grocy-key",
        default=os.environ.get("GROCY_API_KEY", ""),
        metavar="KEY",
        help=(
            "Grocy API key.  "
            "Also read from the GROCY_API_KEY environment variable."
        ),
    )
    parser.add_argument(
        "--location-id",
        type=int,
        default=_env_int("GROCY_LOCATION_ID"),
        metavar="ID",
        help=(
            "Grocy location ID to assign to new products (required). "
            "Also read from the GROCY_LOCATION_ID environment variable."
        ),
    )
    parser.add_argument(
        "--quantity-unit-id",
        type=int,
        default=_env_int("GROCY_QUANTITY_UNIT_ID"),
        metavar="ID",
        help=(
            "Grocy quantity unit ID to assign to new products. "
            "Also read from the GROCY_QUANTITY_UNIT_ID environment variable."
        ),
    )

    # Barcode Buddy options (for --discover)
    parser.add_argument(
        "--bbuddy-url",
        default=os.environ.get("BARCODEBDY_URL", ""),
        metavar="URL",
        help=(
            "Base URL of the Barcode Buddy instance "
            "(e.g. https://bbuddy.example.com).  "
            "Also read from the BARCODEBDY_URL environment variable."
        ),
    )
    parser.add_argument(
        "--bbuddy-key",
        default=os.environ.get("BARCODEBDY_API", ""),
        metavar="KEY",
        help=(
            "Barcode Buddy API key (for /api endpoints).  "
            "Also read from the BARCODEBDY_API environment variable."
        ),
    )
    parser.add_argument(
        "--bbuddy-user",
        default=os.environ.get("BARCODEBDY_USER", ""),
        metavar="USER",
        help=(
            "Barcode Buddy web UI username.  "
            "Also read from the BARCODEBDY_USER environment variable."
        ),
    )
    parser.add_argument(
        "--bbuddy-password",
        default=os.environ.get("BARCODEBDY_PASSWORD", ""),
        metavar="PASS",
        help=(
            "Barcode Buddy web UI password.  "
            "Also read from the BARCODEBDY_PASSWORD environment variable."
        ),
    )

    # AI analysis flags
    parser.add_argument(
        "--sort",
        action="store_true",
        default=False,
        help=(
            "Use Gemini AI to analyse the Grocy products database and assign each "
            "product to the most appropriate available location.  "
            "Requires --grocy-url, --grocy-key, and the GEMINI_API environment variable."
        ),
    )
    parser.add_argument(
        "--date",
        action="store_true",
        default=False,
        help=(
            "Use Gemini AI to guess the default best-before days for each product "
            "in the Grocy database and update the value.  "
            "Requires --grocy-url, --grocy-key, and the GEMINI_API environment variable."
        ),
    )
    parser.add_argument(
        "--group",
        action="store_true",
        default=False,
        help=(
            "Use Gemini AI to analyse the Grocy products database and group "
            "similar products (e.g. different brands of milk) under a shared "
            "parent product.  Creates parent products when needed and enables "
            "'Accumulate sub products min. stock amount' on each parent.  "
            "Requires --grocy-url, --grocy-key, and the GEMINI_API environment variable."
        ),
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        default=False,
        help=(
            "Use Gemini AI to optimize the entire Grocy products database in a "
            "single pass: assign storage locations (sort), estimate best-before "
            "dates, group similar products under parent products, and detect "
            "multi-packs (e.g. '4-pack' → move barcode to base product with "
            "amount=4 and delete the pack product).  "
            "Requires --grocy-url, --grocy-key, and the GEMINI_API environment variable."
        ),
    )
    parser.add_argument(
        "--gemini-api-key",
        default=os.environ.get("GEMINI_API", ""),
        metavar="KEY",
        help=(
            "Gemini API key used for AI-powered --sort, --date, --group, and "
            "--optimize analysis.  "
            "Also read from the GEMINI_API environment variable."
        ),
    )
    parser.add_argument(
        "--gemini-model",
        default=os.environ.get("GEMINI_MODEL", _GEMINI_DEFAULT_MODEL),
        metavar="MODEL",
        help=(
            "Gemini model name to use for --sort, --date, --group, and --optimize "
            f"analysis (default: {_GEMINI_DEFAULT_MODEL}).  "
            "Also read from the GEMINI_MODEL environment variable."
        ),
    )
    parser.add_argument(
        "--searxng-url",
        default=os.environ.get("SEARXNG_URL", ""),
        metavar="URL",
        help=(
            "Base URL of a SearXNG instance for fallback product lookups by EAN "
            "(e.g. http://192.168.1.100:8181).  "
            "Also read from the SEARXNG_URL environment variable."
        ),
    )

    # Behaviour flags
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape products but do not write anything to Grocy.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip products whose EAN is already registered in Grocy (default: true).",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Re-add products even if their EAN is already in Grocy.",
    )
    parser.add_argument(
        "--no-images",
        dest="upload_images",
        action="store_false",
        default=True,
        help="Skip downloading and uploading product images to Grocy.",
    )
    parser.add_argument(
        "--no-graphql",
        dest="use_graphql",
        action="store_false",
        default=True,
        help=(
            "Use the kr-api REST backend (www.k-ruoka.fi/kr-api) instead of "
            "the default GraphQL backend (mobile.k-ruoka.fi/graphql).  "
            "The kr-api backend requires a Cloudflare bypass; "
            "see FLARESOLVERR_URL / CF_CLEARANCE in .env.example."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )

    return parser.parse_args(argv)


def sync_product(
    product: Product,
    grocy: GrocyClient,
    *,
    location_id: int | None,
    quantity_unit_id: int | None,
    skip_existing: bool,
    known_barcodes: set[str],
    upload_images: bool = True,
) -> bool:
    """Add *product* to Grocy.

    Returns ``True`` if the product was created/updated, ``False`` if skipped.
    """
    if not product.ean:
        logger.debug("Skipping '%s' – no EAN code.", product.name)
        return False

    if skip_existing and product.ean in known_barcodes:
        logger.debug("Skipping '%s' – EAN %s already in Grocy.", product.name, product.ean)
        return False

    # Check live against the Grocy API in case known_barcodes is stale.
    try:
        existing = grocy.get_product_by_barcode(product.ean)
    except GrocyAPIError as exc:
        logger.warning("Could not check barcode %s in Grocy: %s", product.ean, exc)
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

    # Create the product entry.
    try:
        grocy_id = grocy.create_product(
            name=product.name,
            description=product.description,
            location_id=location_id,
            quantity_unit_id=quantity_unit_id,
        )
        logger.info("Created product '%s' (Grocy ID %d).", product.name, grocy_id)
    except GrocyAPIError as exc:
        logger.error("Failed to create product '%s': %s", product.name, exc)
        return False

    # Attach the EAN barcode.
    try:
        grocy.add_barcode(grocy_id, product.ean, quantity_unit_id=quantity_unit_id)
        known_barcodes.add(product.ean)
        logger.info("  → Added barcode %s.", product.ean)
    except GrocyAPIError as exc:
        logger.error(
            "Product '%s' created but failed to add barcode %s: %s",
            product.name,
            product.ean,
            exc,
        )

    # Upload product image.
    if upload_images and product.image_url:
        _upload_product_image(product, grocy, grocy_id)

    return True


def _upload_product_image(product: Product, grocy: GrocyClient, grocy_id: int) -> None:
    """Download the product image and upload it to Grocy."""
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
        grocy.upload_product_image(
            grocy_id, filename, resp.content, content_type=content_type
        )
        logger.info("  → Uploaded image %s.", filename)
    except GrocyAPIError as exc:
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


# ---------------------------------------------------------------------------
# Gemini AI helpers
# ---------------------------------------------------------------------------

_GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
)
_GEMINI_DEFAULT_MODEL = "gemini-1.5-flash"
_GEMINI_BATCH_SIZE = 100
_GEMINI_OPTIMIZE_BATCH_SIZE = 1000
_GEMINI_MAX_RETRIES = 3


def _call_gemini(prompt: str, api_key: str, model: str = _GEMINI_DEFAULT_MODEL) -> str:
    """Send *prompt* to the Gemini API and return the text response.

    Parameters
    ----------
    prompt:
        The text prompt to send.
    api_key:
        A valid Gemini API key (from the GEMINI_API env var).
    model:
        The Gemini model name to use (from the GEMINI_MODEL env var).
    """
    url = f"{_GEMINI_BASE_URL}{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            params={"key": api_key},
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except requests.HTTPError as exc:
        raise GrocyAPIError(f"Gemini API error: {exc}") from exc
    except requests.RequestException as exc:
        raise GrocyAPIError(f"Gemini request failed: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise GrocyAPIError(f"Unexpected Gemini response format: {exc}") from exc


def _call_gemini_json(
    prompt: str,
    api_key: str,
    model: str = _GEMINI_DEFAULT_MODEL,
    *,
    max_retries: int = _GEMINI_MAX_RETRIES,
) -> dict:
    """Call Gemini, sanitize the response, and parse it as JSON.

    Retries up to *max_retries* times with exponential back-off when the
    response contains invalid control characters, is HTML instead of JSON,
    or any other transient error occurs.
    """
    max_retries = max(max_retries, 1)
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            raw = _call_gemini(prompt, api_key, model)
            # Strip control characters (except common whitespace) that
            # Gemini occasionally embeds in its output.
            sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
            return json.loads(sanitized)
        except (GrocyAPIError, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = 2 ** attempt
                logger.warning(
                    "Gemini attempt %d/%d failed (%s), retrying in %ds …",
                    attempt,
                    max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _ai_sort_products(grocy: GrocyClient, gemini_api_key: str, model: str = _GEMINI_DEFAULT_MODEL, *, product_ids: list[int] | None = None) -> int:
    """Use Gemini AI to assign each Grocy product to an appropriate location.

    Fetches all locations and products from Grocy, asks Gemini to map each
    product to a location, then updates the products in Grocy.

    When *product_ids* is given, only those products are processed instead of
    the entire Grocy catalogue.

    Returns the number of products updated.
    """
    try:
        locations = grocy.get_locations()
    except GrocyAPIError as exc:
        logger.error("Could not fetch locations from Grocy: %s", exc)
        return 0

    if not locations:
        logger.warning("No locations found in Grocy – skipping --sort.")
        return 0

    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Could not fetch products from Grocy: %s", exc)
        return 0

    if product_ids is not None:
        allowed = set(product_ids)
        products = [p for p in products if int(p["id"]) in allowed]

    if not products:
        logger.info("No products found in Grocy – nothing to sort.")
        return 0

    location_names = {loc["id"]: loc.get("name", str(loc["id"])) for loc in locations}
    location_lines = "\n".join(
        f"  {loc['id']}: {loc.get('name', loc['id'])}" for loc in locations
    )
    logger.info(
        "Asking Gemini to assign locations for %d product(s) …", len(products)
    )

    updated = 0
    for i in range(0, len(products), _GEMINI_BATCH_SIZE):
        batch = products[i : i + _GEMINI_BATCH_SIZE]
        product_lines = "\n".join(
            f"  {p['id']}: {p.get('name', p['id'])}" for p in batch
        )
        prompt = (
            "You are a grocery storage expert helping to organise a home pantry.\n\n"
            "Available storage locations:\n"
            f"{location_lines}\n\n"
            "For each product below, choose the single most appropriate location ID "
            "from the list above.\n"
            "Consider: dairy / meat / fresh produce / drinks / energy drinks / soda → refrigerator; "
            "cleaning / laundry supplies → cleaning cabinet; "
            "dry goods / canned / packaged / eggs → cupboard / pantry.\n\n"
            "Return ONLY a JSON object mapping product IDs (as strings) to "
            "location IDs (as integers), e.g. {\"1\": 2, \"5\": 4}.\n\n"
            "Products:\n"
            f"{product_lines}"
        )
        try:
            mapping: dict = _call_gemini_json(prompt, gemini_api_key, model)
        except (GrocyAPIError, json.JSONDecodeError, ValueError) as exc:
            logger.error("Gemini sort batch %d failed: %s", i // _GEMINI_BATCH_SIZE + 1, exc)
            continue

        for product in batch:
            pid = str(product["id"])
            location_id = mapping.get(pid)
            if location_id is None:
                logger.debug("No location assigned for product %s ('%s').", pid, product.get("name"))
                continue
            try:
                grocy.update_product(int(product["id"]), location_id=int(location_id))
                logger.info(
                    "  → Set location '%s' for '%s' (ID %s).",
                    location_names.get(int(location_id), location_id), product.get("name"), pid,
                )
                updated += 1
            except (GrocyAPIError, ValueError) as exc:
                logger.warning(
                    "Could not update location for '%s': %s", product.get("name"), exc
                )
                continue

            # Move any existing stock to the newly assigned location.
            try:
                stock_locs = grocy.get_product_stock_locations(int(product["id"]))
            except GrocyAPIError as exc:
                logger.debug(
                    "Could not fetch stock locations for '%s': %s",
                    product.get("name"), exc,
                )
                continue

            target = int(location_id)
            for entry in stock_locs:
                entry_loc = int(entry.get("location_id", 0))
                entry_amount = float(entry.get("amount", 0))
                if entry_loc == target or entry_amount <= 0:
                    continue
                try:
                    grocy.transfer_stock(
                        int(product["id"]), entry_amount, entry_loc, target,
                    )
                    logger.info(
                        "    ↳ Moved %.4g unit(s) from '%s' → '%s' for '%s'.",
                        entry_amount,
                        location_names.get(entry_loc, entry_loc),
                        location_names.get(target, target),
                        product.get("name"),
                    )
                except GrocyAPIError as exc:
                    logger.warning(
                        "Could not transfer stock for '%s': %s",
                        product.get("name"), exc,
                    )

    logger.info("--sort complete: %d product(s) updated.", updated)
    return updated


def _ai_assign_due_dates(grocy: GrocyClient, gemini_api_key: str, model: str = _GEMINI_DEFAULT_MODEL, *, product_ids: list[int] | None = None) -> int:
    """Use Gemini AI to set default best-before days for each Grocy product.

    Fetches all products from Grocy, asks Gemini to estimate typical best-before
    days for each, then updates the products in Grocy.

    When *product_ids* is given, only those products are processed instead of
    the entire Grocy catalogue.

    Returns the number of products updated.
    """
    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Could not fetch products from Grocy: %s", exc)
        return 0

    if product_ids is not None:
        allowed = set(product_ids)
        products = [p for p in products if int(p["id"]) in allowed]

    if not products:
        logger.info("No products found in Grocy – nothing to date.")
        return 0

    logger.info(
        "Asking Gemini to estimate due dates for %d product(s) …", len(products)
    )

    updated = 0
    for i in range(0, len(products), _GEMINI_BATCH_SIZE):
        batch = products[i : i + _GEMINI_BATCH_SIZE]
        product_lines = "\n".join(
            f"  {p['id']}: {p.get('name', p['id'])}" for p in batch
        )
        prompt = (
            "You are a grocery best-before date expert.\n\n"
            "For each product below, estimate the typical number of days until the "
            "best-before date for an unopened product stored under normal home "
            "conditions.\n"
            "Guidelines: fresh milk ≈ 7–14 days; yogurt ≈ 21 days; butter ≈ 90 days; "
            "hard cheese ≈ 180 days; eggs ≈ 28 days; bread ≈ 7 days; "
            "canned goods ≈ 730 days; dry pasta / rice ≈ 1095 days; "
            "cooking oil ≈ 365 days; frozen products ≈ 730 days; "
            "cleaning / laundry products ≈ 1095 days.\n\n"
            "Return ONLY a JSON object mapping product IDs (as strings) to days "
            "(as integers), e.g. {\"1\": 14, \"5\": 730}.\n\n"
            "Products:\n"
            f"{product_lines}"
        )
        try:
            mapping: dict = _call_gemini_json(prompt, gemini_api_key, model)
        except (GrocyAPIError, json.JSONDecodeError, ValueError) as exc:
            logger.error("Gemini date batch %d failed: %s", i // _GEMINI_BATCH_SIZE + 1, exc)
            continue

        for product in batch:
            pid = str(product["id"])
            days = mapping.get(pid)
            if days is None:
                logger.debug("No due days assigned for product %s ('%s').", pid, product.get("name"))
                continue
            try:
                grocy.update_product(
                    int(product["id"]), default_best_before_days=int(days)
                )
                logger.info(
                    "  → Set %d best-before days for '%s' (ID %s).",
                    days, product.get("name"), pid,
                )
                updated += 1
            except (GrocyAPIError, ValueError) as exc:
                logger.warning(
                    "Could not update due days for '%s': %s", product.get("name"), exc
                )

    logger.info("--date complete: %d product(s) updated.", updated)
    return updated


def _deduplicate_parent_products(
    grocy: GrocyClient,
    gemini_api_key: str,
    model: str = _GEMINI_DEFAULT_MODEL,
) -> tuple[int, dict[str, str]]:
    """Merge synonymous parent products so only canonical names survive.

    Collects all parent product names (products that have at least one
    child), asks Gemini to cluster them into synonym groups, then moves
    every child of non-canonical parents to the canonical parent and
    deletes the non-canonical ones.

    Returns ``(merged_count, redirect_map)`` where *redirect_map* maps
    each merged-away parent name to its canonical replacement (e.g.
    ``{"Karkki": "Makeiset", "Mauste": "Mausteet"}``).  Callers should
    use the map to redirect any Gemini-suggested group name that matches
    a merged-away parent to the canonical name, preventing recreation of
    deleted parents.
    """
    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Could not fetch products for dedup: %s", exc)
        return 0, {}

    # Build parent→children mapping.
    children_of: dict[int, list[dict]] = {}
    for p in products:
        ppid = p.get("parent_product_id")
        if ppid:
            children_of.setdefault(int(ppid), []).append(p)

    # Collect parent products (those with children).
    parent_products: dict[int, dict] = {}
    for p in products:
        pid = int(p["id"])
        if pid in children_of:
            parent_products[pid] = p

    if len(parent_products) < 2:
        return 0, {}  # Nothing to deduplicate.

    parent_names = sorted(
        p.get("name", f"ID-{pid}") for pid, p in parent_products.items()
    )
    name_list = ", ".join(f'"{n}"' for n in parent_names)

    prompt = (
        "You are a grocery database expert.  Below is a list of product "
        "category names used in a home pantry database.  Some names are "
        "synonyms, near-duplicates, or overly specific variants of the same "
        "category (e.g. \"Mauste\", \"Mausteet\", \"Mausteseos\" all refer "
        "to spices).\n\n"
        "For each name, return the single canonical (preferred) Finnish "
        "category name that group should use.  If a name is already the "
        "best canonical form, map it to itself.  Prefer the most commonly "
        "used, general Finnish grocery term.\n\n"
        "Rules:\n"
        "- Merge true synonyms (Mauste/Mausteet → Mausteet, "
        "Suklaapatukka/Suklaakonvehti/Suklaavohveli → Suklaa if they "
        "are all just chocolate products).\n"
        "- Do NOT merge categories that are genuinely different "
        "(e.g. \"Mustapippuri\" and \"Mausteet\" should stay separate "
        "only if you believe they warrant distinct groups — otherwise "
        "merge specific spice names into the general spice group).\n"
        "- The canonical name MUST be one of the names in the input list "
        "OR a name already used by one of them — do not invent new names.\n\n"
        "Return ONLY a JSON object mapping each input name (string) to its "
        "canonical name (string), e.g.\n"
        '{\"Mauste\": \"Mausteet\", \"Mausteet\": \"Mausteet\", '
        '\"Mausteseos\": \"Mausteet\", \"Leipä\": \"Leipä\"}\n\n'
        f"Category names:\n  {name_list}"
    )

    try:
        mapping: dict = _call_gemini_json(prompt, gemini_api_key, model)
    except (GrocyAPIError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Gemini dedup call failed: %s", exc)
        return 0, {}

    # Build canonical → [non-canonical IDs] from the mapping.
    # We need name→product lookup.
    name_to_parent: dict[str, dict] = {}
    for pid, p in parent_products.items():
        name_to_parent[p.get("name", "")] = p

    # Group: canonical_name → list of (non-canonical) parent products.
    canonical_groups: dict[str, list[dict]] = {}
    for name, canonical in mapping.items():
        if not isinstance(canonical, str) or not canonical:
            continue
        if name == canonical:
            continue  # Already canonical — skip.
        parent = name_to_parent.get(name)
        if parent is None:
            continue
        canonical_groups.setdefault(canonical, []).append(parent)

    if not canonical_groups:
        logger.debug("No duplicate parent groups detected by Gemini.")
        return 0, {}

    merged = 0
    redirect_map: dict[str, str] = {}  # merged-away name → canonical name
    for canonical_name, non_canonical_parents in canonical_groups.items():
        # Find the canonical parent product.
        canonical_parent = name_to_parent.get(canonical_name)
        if canonical_parent is None:
            logger.debug(
                "Canonical parent '%s' not found — skipping merge.",
                canonical_name,
            )
            continue
        canonical_id = int(canonical_parent["id"])

        for dup_parent in non_canonical_parents:
            dup_id = int(dup_parent["id"])
            dup_name = dup_parent.get("name", "?")
            dup_children = children_of.get(dup_id, [])

            # Move children to canonical parent.
            for child in dup_children:
                child_id = int(child["id"])
                try:
                    grocy.update_product(child_id, parent_product_id=canonical_id)
                    logger.info(
                        "  → Moved '%s' (ID %d) from '%s' → '%s'.",
                        child.get("name", "?"), child_id,
                        dup_name, canonical_name,
                    )
                except GrocyAPIError as exc:
                    logger.warning(
                        "Could not move child '%s' to '%s': %s",
                        child.get("name", "?"), canonical_name, exc,
                    )

            # Delete the now-empty non-canonical parent.
            try:
                picture = dup_parent.get("picture_file_name", "")
                if picture:
                    try:
                        grocy.delete_product_image(picture)
                    except GrocyAPIError:
                        pass
                grocy.delete_product(dup_id)
                logger.info(
                    "  → Merged duplicate parent '%s' (ID %d) into '%s'.",
                    dup_name, dup_id, canonical_name,
                )
                merged += 1
                redirect_map[dup_name] = canonical_name
            except GrocyAPIError as exc:
                logger.warning(
                    "Could not delete duplicate parent '%s': %s",
                    dup_name, exc,
                )

    if merged:
        logger.info("Deduplicated %d parent product(s).", merged)
    return merged, redirect_map


def _ai_group_products(
    grocy: GrocyClient,
    gemini_api_key: str,
    model: str = _GEMINI_DEFAULT_MODEL,
    *,
    location_id: int | None = None,
    quantity_unit_id: int | None = None,
    product_ids: list[int] | None = None,
) -> int:
    """Use Gemini AI to group similar products under shared parent products.

    Fetches all products from Grocy, asks Gemini to identify groups of
    similar items (e.g. different brands of milk), creates parent products
    where needed, and assigns each child product to its parent.
    Parent products are created with
    ``cumulate_min_stock_amount_of_sub_products`` enabled.  Each parent is
    also assigned to the ``"Group master"`` product group and marked with
    ``hide_on_stock_overview`` so it does not clutter the stock overview.

    Gemini returns two levels for each product:

    * **parent** — a specific parent product name (e.g. ``"Mustapippuri"``)
    * **category** — a broad product group (e.g. ``"Mausteet"``)

    The parent is used for Grocy parent-product assignment (many, detailed).
    The category is used for Grocy product-group assignment (few, general).

    When *product_ids* is given, only those products are considered for
    grouping instead of the entire Grocy catalogue.

    Returns the number of products updated.
    """
    # Consolidate duplicate parent products before grouping.
    _dedup_count, dedup_map = _deduplicate_parent_products(grocy, gemini_api_key, model)

    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Could not fetch products from Grocy: %s", exc)
        return 0

    if not products:
        logger.info("No products found in Grocy – nothing to group.")
        return 0

    # Ensure the "Group master" product group exists so we can assign parents.
    group_master_id: int | None = None
    try:
        group_master_id = grocy.ensure_product_group("Group master")
    except GrocyAPIError as exc:
        logger.warning(
            "Could not ensure 'Group master' product group: %s", exc,
        )

    # Build a set of product IDs that already act as parents (have
    # sub-products).  Grocy only supports one nesting level, so these
    # products must not be assigned a parent themselves.
    has_children: set[int] = set()
    for p in products:
        ppid = p.get("parent_product_id")
        if ppid:
            has_children.add(int(ppid))

    # Collect existing parent product names so Gemini can reuse them.
    existing_parent_names: list[str] = sorted({
        p.get("name", "")
        for p in products
        if int(p["id"]) in has_children and p.get("name")
    })

    # Only consider products that do not already have a parent and are not
    # already parents of sub-products (assigning a parent to a product that
    # already has children would violate Grocy's single-level nesting limit).
    ungrouped = [
        p for p in products
        if not p.get("parent_product_id") and int(p["id"]) not in has_children
    ]
    if product_ids is not None:
        allowed = set(product_ids)
        ungrouped = [p for p in ungrouped if int(p["id"]) in allowed]
    if not ungrouped:
        logger.info("All products already have parent products – nothing to group.")
        return 0

    # Build a name→id index of existing products so we can reuse parents.
    name_to_product: dict[str, dict] = {}
    for p in products:
        name_to_product[p.get("name", "")] = p

    # Collect existing product group names so Gemini can reuse categories.
    existing_category_names: list[str] = []
    try:
        all_groups = grocy.get_product_groups()
        existing_category_names = sorted({
            g.get("name", "")
            for g in all_groups
            if g.get("name") and g.get("name") != "Group master"
        })
    except GrocyAPIError:
        pass

    logger.info(
        "Asking Gemini to group %d product(s) …", len(ungrouped)
    )

    updated = 0
    for i in range(0, len(ungrouped), _GEMINI_BATCH_SIZE):
        batch = ungrouped[i : i + _GEMINI_BATCH_SIZE]
        product_lines = "\n".join(
            f"  {p['id']}: {p.get('name', p['id'])}" for p in batch
        )

        existing_parents_section = ""
        if existing_parent_names:
            existing_parents_lines = ", ".join(
                f'"{n}"' for n in existing_parent_names
            )
            existing_parents_section = (
                "Existing parent products (reuse these exact names when "
                "a product fits — do NOT create synonyms or variants like "
                '"Mauste" when "Mausteet" already exists):\n'
                f"  {existing_parents_lines}\n\n"
            )

        existing_categories_section = ""
        if existing_category_names:
            existing_categories_lines = ", ".join(
                f'"{n}"' for n in existing_category_names
            )
            existing_categories_section = (
                "Existing product categories (reuse these exact names "
                "when a product fits):\n"
                f"  {existing_categories_lines}\n\n"
            )

        prompt = (
            "You are a grocery database expert helping to organise a product "
            "catalogue.\n\n"
            f"{existing_parents_section}"
            f"{existing_categories_section}"
            "For each product below, return a JSON object with TWO levels of "
            "grouping:\n\n"
            '1. "parent" — a SPECIFIC parent product name in Finnish that '
            "closely matches the product type. Be detailed: use separate "
            'parents for each distinct product (e.g. "Mustapippuri" for '
            'black pepper, "Oregano" for oregano, "Maito" for milk, '
            '"Suklaa" for chocolate, "Sipsi" for chips). '
            "If an existing parent product name fits, you MUST use that "
            "exact name.\n\n"
            '2. "category" — a BROAD product category in Finnish that covers '
            "many related parents. Categories should be few and general "
            '(e.g. "Mausteet" covers all spices like Mustapippuri, Oregano, '
            'Timjami; "Makeiset" covers all candy and chocolate; '
            '"Maitotaloustuotteet" covers milk, cream, yogurt; '
            '"Juomat" covers all drinks). '
            "If an existing category name fits, you MUST use that "
            "exact name.\n\n"
            "Group ALL grocery categories including "
            "dairy, eggs, bread, flour, butter, rice, pasta, cooking oil, "
            "canned goods, frozen vegetables, meat, snacks, candy, "
            "soft drinks, energy drinks, alcoholic beverages, etc.\n"
            "If a product should NOT be grouped, map it to null.\n\n"
            "Return ONLY a JSON object mapping product IDs (as strings) to "
            "objects or null, e.g.\n"
            '{"1": {"parent": "Maito", "category": "Maitotaloustuotteet"}, '
            '"2": {"parent": "Mustapippuri", "category": "Mausteet"}, '
            '"7": null}.\n\n'
            "Products:\n"
            f"{product_lines}"
        )
        try:
            mapping: dict = _call_gemini_json(prompt, gemini_api_key, model)
        except (GrocyAPIError, json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "Gemini group batch %d failed: %s",
                i // _GEMINI_BATCH_SIZE + 1, exc,
            )
            continue

        # Collect unique parent names and category names from this batch.
        # Redirect merged-away parent names to their canonical replacements.
        parent_names: set[str] = set()
        category_names: set[str] = set()
        for product in batch:
            pid = str(product["id"])
            entry = mapping.get(pid)
            if not isinstance(entry, dict):
                continue
            parent_name = entry.get("parent")
            if parent_name:
                parent_name = dedup_map.get(str(parent_name), str(parent_name))
                entry["parent"] = parent_name
                parent_names.add(parent_name)
            cat_name = entry.get("category")
            if cat_name:
                category_names.add(str(cat_name))

        # Ensure each parent product exists.
        parent_name_to_id: dict[str, int] = {}
        for parent_name in parent_names:
            existing = name_to_product.get(parent_name)
            if existing and not existing.get("parent_product_id"):
                parent_id = int(existing["id"])
                parent_name_to_id[parent_name] = parent_id
            elif existing:
                logger.debug(
                    "Skipping '%s' as parent – already a child product.",
                    parent_name,
                )
                continue
            else:
                try:
                    parent_id = grocy.create_product(
                        parent_name,
                        location_id=location_id,
                        quantity_unit_id=quantity_unit_id,
                    )
                    logger.info(
                        "  → Created parent product '%s' (ID %d).",
                        parent_name, parent_id,
                    )
                    parent_name_to_id[parent_name] = parent_id
                    name_to_product[parent_name] = {"id": parent_id, "name": parent_name}
                except GrocyAPIError as exc:
                    logger.warning(
                        "Could not create parent product '%s': %s",
                        parent_name, exc,
                    )
                    continue

            # Configure the parent: accumulate sub-product stock, assign to
            # "Group master" product group, and hide from the stock overview.
            parent_update: dict = {
                "cumulate_min_stock_amount_of_sub_products": 1,
                "hide_on_stock_overview": 1,
            }
            if group_master_id is not None:
                parent_update["product_group_id"] = group_master_id
            try:
                grocy.update_product(
                    parent_name_to_id[parent_name], **parent_update,
                )
            except GrocyAPIError as exc:
                logger.warning(
                    "Could not update parent product '%s': %s",
                    parent_name, exc,
                )

        # Ensure each broad category product group exists.
        category_name_to_group_id: dict[str, int] = {}
        for cat_name in category_names:
            try:
                category_name_to_group_id[cat_name] = (
                    grocy.ensure_product_group(cat_name)
                )
            except GrocyAPIError as exc:
                logger.warning(
                    "Could not ensure product group '%s': %s",
                    cat_name, exc,
                )

        # Assign child products to their parents with broad category.
        for product in batch:
            pid = str(product["id"])
            entry = mapping.get(pid)
            if not isinstance(entry, dict):
                logger.debug(
                    "No group assigned for product %s ('%s').",
                    pid, product.get("name"),
                )
                continue
            parent_name = entry.get("parent")
            if not parent_name:
                continue
            parent_id = parent_name_to_id.get(str(parent_name))
            if parent_id is None:
                continue
            # Don't set a product as its own parent.
            if int(product["id"]) == parent_id:
                continue
            child_update: dict = {"parent_product_id": parent_id}
            cat_name = entry.get("category")
            if cat_name:
                child_group_id = category_name_to_group_id.get(str(cat_name))
                if child_group_id is not None:
                    child_update["product_group_id"] = child_group_id
            try:
                grocy.update_product(int(product["id"]), **child_update)
                logger.info(
                    "  → Grouped '%s' (ID %s) under '%s'.",
                    product.get("name"), pid, parent_name,
                )
                updated += 1
            except (GrocyAPIError, ValueError) as exc:
                logger.warning(
                    "Could not group '%s': %s", product.get("name"), exc
                )

    logger.info("--group complete: %d product(s) grouped.", updated)
    return updated


def _ai_optimize_products(
    grocy: GrocyClient,
    gemini_api_key: str,
    model: str = _GEMINI_DEFAULT_MODEL,
    *,
    location_id: int | None = None,
    quantity_unit_id: int | None = None,
    product_ids: list[int] | None = None,
) -> int:
    """Use Gemini AI to optimize the Grocy product database in a single pass.

    Combines sorting (location assignment), best-before date estimation,
    product grouping, and multi-pack detection into one Gemini prompt per
    batch.  Uses a larger batch size (1000) to give the model a broad view
    of the catalogue.

    **Pack handling**: when the model identifies a product as a multi-pack
    (e.g. "Red Bull 4-pack"), the pack product's barcode is moved to the
    base product with ``amount = pack_count``, and the pack product is
    deleted from Grocy.

    When *product_ids* is given, only those products are processed.

    Returns the number of products updated.
    """
    # --- Fetch locations -------------------------------------------------
    try:
        locations = grocy.get_locations()
    except GrocyAPIError as exc:
        logger.error("Could not fetch locations from Grocy: %s", exc)
        return 0

    location_names: dict[int, str] = {}
    location_lines = ""
    if locations:
        location_names = {
            int(loc["id"]): loc.get("name", str(loc["id"])) for loc in locations
        }
        location_lines = "\n".join(
            f"  {loc['id']}: {loc.get('name', loc['id'])}" for loc in locations
        )

    # --- Deduplicate parent products first ---------------------------------
    _dedup_count, dedup_map = _deduplicate_parent_products(grocy, gemini_api_key, model)

    # --- Fetch products --------------------------------------------------
    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Could not fetch products from Grocy: %s", exc)
        return 0

    if not products:
        logger.info("No products found in Grocy – nothing to optimize.")
        return 0

    # Build a name→product index (used for grouping & pack handling).
    name_to_product: dict[str, dict] = {}
    for p in products:
        name_to_product[p.get("name", "")] = p

    # Determine which products already act as parents (have children).
    has_children: set[int] = set()
    for p in products:
        ppid = p.get("parent_product_id")
        if ppid:
            has_children.add(int(ppid))

    # Collect existing parent product names so Gemini can reuse them.
    existing_parent_names: list[str] = sorted({
        p.get("name", "")
        for p in products
        if int(p["id"]) in has_children and p.get("name")
    })

    # Collect existing product group names so Gemini can reuse categories.
    existing_category_names: list[str] = []
    try:
        all_groups = grocy.get_product_groups()
        existing_category_names = sorted({
            g.get("name", "")
            for g in all_groups
            if g.get("name") and g.get("name") != "Group master"
        })
    except GrocyAPIError:
        pass
    if product_ids is not None:
        allowed = set(product_ids)
        products = [p for p in products if int(p["id"]) in allowed]

    if not products:
        logger.info("No matching products – nothing to optimize.")
        return 0

    # Ensure "Group master" product group for parent products.
    group_master_id: int | None = None
    try:
        group_master_id = grocy.ensure_product_group("Group master")
    except GrocyAPIError as exc:
        logger.warning("Could not ensure 'Group master' product group: %s", exc)

    logger.info("Asking Gemini to optimize %d product(s) …", len(products))

    updated = 0
    deleted_ids: set[int] = set()
    for i in range(0, len(products), _GEMINI_OPTIMIZE_BATCH_SIZE):
        batch = products[i : i + _GEMINI_OPTIMIZE_BATCH_SIZE]
        product_lines = "\n".join(
            f"  {p['id']}: {p.get('name', p['id'])}" for p in batch
        )

        location_section = ""
        if location_lines:
            location_section = (
                "Available storage locations:\n"
                f"{location_lines}\n\n"
            )

        existing_parents_section = ""
        if existing_parent_names:
            existing_parents_lines = ", ".join(
                f'"{n}"' for n in existing_parent_names
            )
            existing_parents_section = (
                "Existing parent products (reuse these exact names when "
                "a product fits — do NOT create synonyms or variants like "
                '"Mauste" when "Mausteet" already exists):\n'
                f"  {existing_parents_lines}\n\n"
            )

        existing_categories_section = ""
        if existing_category_names:
            existing_categories_lines = ", ".join(
                f'"{n}"' for n in existing_category_names
            )
            existing_categories_section = (
                "Existing product categories (reuse these exact names "
                "when a product fits):\n"
                f"  {existing_categories_lines}\n\n"
            )

        prompt = (
            "You are a grocery database expert helping to organise a home pantry.\n\n"
            f"{location_section}"
            f"{existing_parents_section}"
            f"{existing_categories_section}"
            "For each product below, return a JSON object mapping the product ID "
            "(as a string) to an object with these fields:\n"
            '  "location_id": (integer) the most appropriate storage location ID '
            "from the list above, or null if no locations are available.\n"
            '  "best_before_days": (integer) estimated days until best-before date '
            "for an unopened product under normal home storage.\n"
            '  "group_name": (string) a SPECIFIC Finnish parent product name '
            "that closely matches the product type. Be detailed: use separate "
            'parents for each distinct product (e.g. "Mustapippuri" for '
            'black pepper, "Oregano" for oregano, "Maito" for milk). '
            "If an existing parent product name fits, you MUST use that "
            "exact name. Null if the product is unique.\n"
            '  "category": (string) a BROAD Finnish product category that covers '
            "many related parent products. Categories should be few and general "
            '(e.g. "Mausteet" for all spices, "Makeiset" for all candy, '
            '"Maitotaloustuotteet" for all dairy, "Juomat" for all drinks). '
            "If an existing category name fits, you MUST use that exact name. "
            "Null if the product is unique.\n"
            '  "pack_of": (string) the base product name if this product is a '
            "multi-pack (e.g. for \"Red Bull 4-pack\" → \"Red Bull\"), or null "
            "if it is not a pack.\n"
            '  "pack_count": (integer) the number of individual items in the pack '
            "(e.g. 4 for a 4-pack), or null if not a pack.\n\n"
            "Guidelines:\n"
            "- Location: dairy/meat/fresh produce/drinks/energy drinks/soda → refrigerator; "
            "cleaning/laundry → cleaning cabinet; dry goods/canned/packaged/eggs → cupboard/pantry.\n"
            "- Best-before: fresh milk ≈ 7–14d; yogurt ≈ 21d; butter ≈ 90d; "
            "hard cheese ≈ 180d; eggs ≈ 28d; bread ≈ 7d; canned ≈ 730d; "
            "dry pasta/rice ≈ 1095d; cooking oil ≈ 365d; frozen ≈ 730d; "
            "cleaning/laundry ≈ 1095d.\n"
            "- Grouping: group ALL grocery categories.  If an existing parent "
            "product name fits, you MUST use that exact name.\n"
            "- Packs: detect multi-packs from names like '4-pack', '6x0.33l', "
            "'monipakkaus', '4 kpl', etc.\n\n"
            "Return ONLY valid JSON, for example:\n"
            '{"1": {"location_id": 2, "best_before_days": 14, '
            '"group_name": "Maito", "category": "Maitotaloustuotteet", '
            '"pack_of": null, "pack_count": null}, '
            '"2": {"location_id": 3, "best_before_days": 730, '
            '"group_name": null, "category": null, '
            '"pack_of": "Red Bull", "pack_count": 4}}\n\n'
            "Products:\n"
            f"{product_lines}"
        )

        try:
            mapping: dict = _call_gemini_json(prompt, gemini_api_key, model)
        except (GrocyAPIError, json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "Gemini optimize batch %d failed: %s",
                i // _GEMINI_OPTIMIZE_BATCH_SIZE + 1, exc,
            )
            continue

        # --- Apply results -----------------------------------------------
        # First pass: collect parent names, category names, and pack base names.
        # Redirect merged-away names to their canonical replacements.
        parent_names: set[str] = set()
        category_names: set[str] = set()
        pack_base_names: set[str] = set()
        for product in batch:
            pid = str(product["id"])
            info = mapping.get(pid)
            if not isinstance(info, dict):
                continue
            gn = info.get("group_name")
            if gn:
                gn = dedup_map.get(str(gn), str(gn))
                info["group_name"] = gn
                parent_names.add(gn)
            cat = info.get("category")
            if cat:
                category_names.add(str(cat))
            po = info.get("pack_of")
            if po:
                pack_base_names.add(str(po))

        # Ensure parent products exist (for grouping).
        parent_name_to_id: dict[str, int] = {}
        for parent_name in parent_names:
            existing = name_to_product.get(parent_name)
            if existing and not existing.get("parent_product_id"):
                parent_name_to_id[parent_name] = int(existing["id"])
            elif existing:
                logger.debug("Skipping '%s' as parent – already a child.", parent_name)
                continue
            else:
                try:
                    pid_new = grocy.create_product(
                        parent_name,
                        location_id=location_id,
                        quantity_unit_id=quantity_unit_id,
                    )
                    logger.info("  → Created parent product '%s' (ID %d).", parent_name, pid_new)
                    parent_name_to_id[parent_name] = pid_new
                    name_to_product[parent_name] = {"id": pid_new, "name": parent_name}
                except GrocyAPIError as exc:
                    logger.warning("Could not create parent product '%s': %s", parent_name, exc)
                    continue

            parent_update: dict = {
                "cumulate_min_stock_amount_of_sub_products": 1,
                "hide_on_stock_overview": 1,
            }
            if group_master_id is not None:
                parent_update["product_group_id"] = group_master_id
            try:
                grocy.update_product(parent_name_to_id[parent_name], **parent_update)
            except GrocyAPIError as exc:
                logger.warning("Could not update parent product '%s': %s", parent_name, exc)

        # Ensure each broad category product group exists.
        category_name_to_group_id: dict[str, int] = {}
        for cat_name in category_names:
            try:
                category_name_to_group_id[cat_name] = (
                    grocy.ensure_product_group(cat_name)
                )
            except GrocyAPIError as exc:
                logger.warning("Could not ensure product group '%s': %s", cat_name, exc)

        # Ensure base products exist (for pack handling).
        pack_base_name_to_id: dict[str, int] = {}
        for base_name in pack_base_names:
            existing = name_to_product.get(base_name)
            if existing:
                pack_base_name_to_id[base_name] = int(existing["id"])
            else:
                try:
                    pid_new = grocy.create_product(
                        base_name,
                        location_id=location_id,
                        quantity_unit_id=quantity_unit_id,
                    )
                    logger.info("  → Created base product '%s' (ID %d) for pack.", base_name, pid_new)
                    pack_base_name_to_id[base_name] = pid_new
                    name_to_product[base_name] = {"id": pid_new, "name": base_name}
                except GrocyAPIError as exc:
                    logger.warning("Could not create base product '%s': %s", base_name, exc)

        # Second pass: apply sort, date, group, and pack for each product.
        for product in batch:
            pid = str(product["id"])
            info = mapping.get(pid)
            if not isinstance(info, dict):
                logger.debug("No optimize info for product %s ('%s').", pid, product.get("name"))
                continue

            product_id = int(product["id"])

            # --- Pack handling (do first, may delete product) -------------
            pack_of = info.get("pack_of")
            pack_count = info.get("pack_count")
            if pack_of and pack_count:
                base_id = pack_base_name_to_id.get(str(pack_of))
                if base_id is not None and base_id != product_id:
                    try:
                        barcodes = grocy.get_product_barcodes(product_id)
                        for bc_entry in barcodes:
                            bc_id = int(bc_entry["id"])
                            grocy.update_barcode(
                                bc_id,
                                product_id=base_id,
                                amount=int(pack_count),
                            )
                            logger.info(
                                "  → Moved barcode '%s' from '%s' to '%s' (amount=%d).",
                                bc_entry.get("barcode", "?"),
                                product.get("name"),
                                pack_of,
                                int(pack_count),
                            )
                        # Delete the pack product.
                        picture = product.get("picture_file_name", "")
                        if picture:
                            try:
                                grocy.delete_product_image(picture)
                            except GrocyAPIError:
                                pass
                        grocy.delete_product(product_id)
                        logger.info(
                            "  → Deleted pack product '%s' (ID %s).",
                            product.get("name"), pid,
                        )
                        deleted_ids.add(product_id)
                        updated += 1
                        continue  # Skip sort/date/group for deleted product.
                    except GrocyAPIError as exc:
                        logger.warning(
                            "Could not handle pack for '%s': %s",
                            product.get("name"), exc,
                        )

            # --- Sort (location assignment) -------------------------------
            loc_id = info.get("location_id")
            if loc_id is not None and locations:
                try:
                    grocy.update_product(product_id, location_id=int(loc_id))
                    logger.info(
                        "  → Set location '%s' for '%s' (ID %s).",
                        location_names.get(int(loc_id), loc_id),
                        product.get("name"), pid,
                    )
                    updated += 1
                except (GrocyAPIError, ValueError) as exc:
                    logger.warning(
                        "Could not update location for '%s': %s",
                        product.get("name"), exc,
                    )

                # Transfer existing stock to the new location.
                try:
                    stock_locs = grocy.get_product_stock_locations(product_id)
                    target = int(loc_id)
                    for entry in stock_locs:
                        entry_loc = int(entry.get("location_id", 0))
                        entry_amount = float(entry.get("amount", 0))
                        if entry_loc == target or entry_amount <= 0:
                            continue
                        grocy.transfer_stock(product_id, entry_amount, entry_loc, target)
                        logger.info(
                            "    ↳ Moved %.4g unit(s) from '%s' → '%s' for '%s'.",
                            entry_amount,
                            location_names.get(entry_loc, entry_loc),
                            location_names.get(target, target),
                            product.get("name"),
                        )
                except GrocyAPIError as exc:
                    logger.debug(
                        "Could not transfer stock for '%s': %s",
                        product.get("name"), exc,
                    )

            # --- Date (best-before days) ----------------------------------
            days = info.get("best_before_days")
            if days is not None:
                try:
                    grocy.update_product(product_id, default_best_before_days=int(days))
                    logger.info(
                        "  → Set %d best-before days for '%s' (ID %s).",
                        int(days), product.get("name"), pid,
                    )
                    updated += 1
                except (GrocyAPIError, ValueError) as exc:
                    logger.warning(
                        "Could not update due days for '%s': %s",
                        product.get("name"), exc,
                    )

            # --- Group (parent product assignment) ------------------------
            group_name = info.get("group_name")
            if group_name:
                parent_id = parent_name_to_id.get(str(group_name))
                current_parent = product.get("parent_product_id")
                current_parent_int = int(current_parent) if current_parent else None
                if (
                    parent_id is not None
                    and parent_id != product_id
                    and parent_id != current_parent_int
                    and product_id not in has_children
                ):
                    child_update: dict = {"parent_product_id": parent_id}
                    cat_name = info.get("category")
                    if cat_name:
                        child_group_id = category_name_to_group_id.get(str(cat_name))
                        if child_group_id is not None:
                            child_update["product_group_id"] = child_group_id
                    try:
                        grocy.update_product(product_id, **child_update)
                        if current_parent_int:
                            logger.info(
                                "  → Re-grouped '%s' (ID %s) from parent %d → '%s'.",
                                product.get("name"), pid,
                                current_parent_int, group_name,
                            )
                        else:
                            logger.info(
                                "  → Grouped '%s' (ID %s) under '%s'.",
                                product.get("name"), pid, group_name,
                            )
                        updated += 1
                    except (GrocyAPIError, ValueError) as exc:
                        logger.warning(
                            "Could not group '%s': %s", product.get("name"), exc,
                        )

    # --- Clean up empty parent products ----------------------------------
    # Re-scan products to find parents that now have zero children.
    try:
        all_products_after = grocy.get_all_products()
    except GrocyAPIError:
        all_products_after = []

    if all_products_after:
        children_of: dict[int, list] = {}
        for p in all_products_after:
            ppid = p.get("parent_product_id")
            if ppid:
                children_of.setdefault(int(ppid), []).append(p)

        for p in all_products_after:
            pid_int = int(p["id"])
            # A product is an empty parent if it was configured as a parent
            # (cumulate sub-product stock + hidden from overview) but has no
            # remaining children.
            if (
                pid_int not in children_of
                and p.get("cumulate_min_stock_amount_of_sub_products") in (1, "1", True)
                and p.get("hide_on_stock_overview") in (1, "1", True)
                and pid_int not in deleted_ids
            ):
                try:
                    picture = p.get("picture_file_name", "")
                    if picture:
                        try:
                            grocy.delete_product_image(picture)
                        except GrocyAPIError:
                            pass
                    grocy.delete_product(pid_int)
                    logger.info(
                        "  → Deleted empty parent '%s' (ID %d).",
                        p.get("name"), pid_int,
                    )
                    updated += 1
                except GrocyAPIError as exc:
                    logger.warning(
                        "Could not delete empty parent '%s': %s",
                        p.get("name"), exc,
                    )

    logger.info("--optimize complete: %d product(s) updated.", updated)
    return updated


def _validate_args(args: argparse.Namespace) -> int:
    """Return 0 if arguments are valid, 1 otherwise."""
    ai_mode = args.sort or args.date or args.group or args.optimize
    scrape_mode = bool(args.query or args.browse)
    discover_mode = args.discover
    delete_all_mode = args.delete_all
    update_mode = args.update

    # At least one operational mode must be selected.
    if not ai_mode and not scrape_mode and not discover_mode and not delete_all_mode and not update_mode:
        logger.error(
            "Specify a scraping mode (--query / --browse), an AI analysis mode "
            "(--sort / --date / --optimize), --discover, --update, or --delete-all."
        )
        return 1

    # Store is required when scraping, discovering, or updating.
    if scrape_mode or discover_mode or update_mode:
        if not _parse_store_ids(args.store):
            logger.error(
                "Store ID is required.  Use --store or set the KRUOKA_STORE_ID "
                "environment variable.  Multiple stores can be comma-separated."
            )
            return 1

    if ai_mode or (scrape_mode and not args.dry_run) or discover_mode or delete_all_mode or update_mode:
        # Grocy connection is required for AI mode, non-dry-run scraping, and discover.
        if not args.grocy_url:
            logger.error(
                "Grocy URL is required.  Use --grocy-url or set GROCY_BASE_URL."
            )
            return 1
        if not args.grocy_key:
            logger.error(
                "Grocy API key is required.  Use --grocy-key or set GROCY_API_KEY."
            )
            return 1

    if ai_mode:
        if not args.gemini_api_key:
            logger.error(
                "Gemini API key is required for --sort / --date / --group / --optimize.  "
                "Use --gemini-api-key or set the GEMINI_API environment variable."
            )
            return 1

    if (scrape_mode and not args.dry_run) or discover_mode:
        if args.location_id is None:
            logger.error(
                "Location ID is required.  Use --location-id or set GROCY_LOCATION_ID."
            )
            return 1
        if args.quantity_unit_id is None:
            logger.error(
                "Quantity unit ID is required.  Use --quantity-unit-id or set GROCY_QUANTITY_UNIT_ID."
            )
            return 1

    if discover_mode:
        if not args.bbuddy_url:
            logger.error(
                "Barcode Buddy URL is required for --discover.  "
                "Use --bbuddy-url or set the BARCODEBDY_URL environment variable."
            )
            return 1
        if not args.bbuddy_user or not args.bbuddy_password:
            logger.error(
                "Barcode Buddy username and password are required for --discover.  "
                "Use --bbuddy-user / --bbuddy-password or set "
                "BARCODEBDY_USER / BARCODEBDY_PASSWORD environment variables."
            )
            return 1

    return 0


def _setup_grocy(args: argparse.Namespace) -> tuple[GrocyClient | None, set[str]]:
    """Create a Grocy client and pre-load known barcodes if not a dry run."""
    if args.dry_run:
        return None, set()

    grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
    known_barcodes: set[str] = set()

    if args.skip_existing:
        logger.info("Fetching existing barcodes from Grocy …")
        try:
            for entry in grocy.get_all_barcodes():
                bc = entry.get("barcode")
                if bc:
                    known_barcodes.add(str(bc))
            logger.info("  %d barcode(s) already registered.", len(known_barcodes))
        except GrocyAPIError as exc:
            logger.warning(
                "Could not fetch existing barcodes (%s); will check each individually.",
                exc,
            )

    return grocy, known_barcodes


def _run_scraper(args: argparse.Namespace):  # type: ignore[return]
    """Yield products from the k-ruoka.fi scraper.

    When multiple store IDs are configured (comma-separated ``--store``),
    the first store is tried; if it raises an exception the next store is
    attempted, and so on.  Results are streamed lazily (not materialised in
    memory).
    """
    store_ids = _parse_store_ids(args.store)
    backend = "GraphQL" if args.use_graphql else "kr-api"

    for idx, store_id in enumerate(store_ids):
        scraper = KRuokaScraper(store_id=store_id, use_graphql=args.use_graphql)
        try:
            if args.query:
                logger.info(
                    "Searching k-ruoka.fi (store=%s, backend=%s) for '%s' …",
                    store_id, backend, args.query,
                )
                yield from scraper.search(args.query, max_products=args.max_products)
            else:
                logger.info(
                    "Browsing k-ruoka.fi catalogue (store=%s, backend=%s) …",
                    store_id, backend,
                )
                yield from scraper.browse(max_products=args.max_products)
            return  # Store succeeded – stop trying.
        except Exception as exc:
            if idx < len(store_ids) - 1:
                logger.warning(
                    "Store %s failed (%s); trying next store …", store_id, exc,
                )
            else:
                raise


def _process_products(args: argparse.Namespace, grocy: GrocyClient | None, known_barcodes: set[str]) -> int:
    """Process scraped products; return 0 on success, 1 if any errors occurred."""
    created = skipped = errors = 0

    for product in _run_scraper(args):
        if args.dry_run:
            ean_display = product.ean or "(no EAN)"
            img_display = f"  IMG:{product.image_url}" if product.image_url else ""
            print(f"{product.name!r}  EAN:{ean_display}{img_display}")
            created += 1
            continue

        assert grocy is not None
        try:
            added = sync_product(
                product,
                grocy,
                location_id=args.location_id,
                quantity_unit_id=args.quantity_unit_id,
                skip_existing=args.skip_existing,
                known_barcodes=known_barcodes,
                upload_images=args.upload_images,
            )
        except GrocyAPIError as exc:
            logger.error("Grocy error for '%s': %s", product.name, exc)
            errors += 1
            continue

        if added:
            created += 1
        else:
            skipped += 1

    if args.dry_run:
        logger.info("Dry run complete – %d product(s) found.", created)
    else:
        logger.info(
            "Done – created: %d  skipped: %d  errors: %d", created, skipped, errors
        )

    return 0 if errors == 0 else 1


def _discover_single_barcode(
    args: argparse.Namespace,
    barcode: str,
) -> dict:
    """Discover a single barcode by searching K-Ruoka / S-kaupat and syncing to Grocy.

    Unlike :func:`_discover_products`, this bypasses Barcode Buddy entirely —
    the caller already knows the barcode.  It searches online stores, creates
    the product in Grocy, adds 1 unit to stock, and returns a result dict.

    Returns ``{"success": True, "product": {...}, "grocy_id": int}`` on
    success, or ``{"success": False, "error": "..."}`` on failure.
    """
    grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
    store_ids = _parse_store_ids(args.store)
    scrapers = [
        KRuokaScraper(store_id=sid, use_graphql=args.use_graphql)
        for sid in store_ids
    ]

    # Check if barcode already exists in Grocy.
    try:
        existing = grocy.get_product_by_barcode(barcode)
        if existing:
            name = existing.get("name", barcode)
            grocy_id = existing.get("id")
            logger.info("Barcode %s already in Grocy as '%s' (ID %s).", barcode, name, grocy_id)
            return {
                "success": True,
                "product": {"name": name, "barcode": barcode},
                "grocy_id": grocy_id,
                "already_existed": True,
            }
    except GrocyAPIError as exc:
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

    # Sync product to Grocy.
    known_barcodes: set[str] = set()
    try:
        added = sync_product(
            product,
            grocy,
            location_id=args.location_id,
            quantity_unit_id=args.quantity_unit_id,
            skip_existing=False,
            known_barcodes=known_barcodes,
            upload_images=args.upload_images,
        )
    except GrocyAPIError as exc:
        logger.error("Grocy error for '%s': %s", product.name, exc)
        return {"success": False, "error": f"Failed to create product in Grocy: {exc}"}

    # Look up the Grocy product ID.
    grocy_id = None
    try:
        existing = grocy.get_product_by_barcode(barcode)
        if existing:
            grocy_id = existing.get("id")
    except GrocyAPIError:
        pass

    # Add 1 unit to stock.
    if grocy_id is not None:
        try:
            grocy.add_stock(int(grocy_id), amount=1.0)
            logger.info("Added 1 unit to Grocy stock (product ID %s).", grocy_id)
        except (GrocyAPIError, ValueError) as exc:
            logger.warning("Could not add stock for '%s': %s", product.name, exc)

    # Remove the barcode from Barcode Buddy's unknown/pending list.
    if getattr(args, "bbuddy_url", "") and getattr(args, "bbuddy_user", ""):
        try:
            bbuddy = BarcodeBuddyClient(
                base_url=args.bbuddy_url,
                api_key=args.bbuddy_key,
                username=args.bbuddy_user,
                password=args.bbuddy_password,
            )
            for entry in bbuddy.get_pending_barcodes():
                if entry.barcode == barcode:
                    bbuddy.delete_barcode(entry.id)
                    logger.info("Removed EAN %s (id %s) from Barcode Buddy.", barcode, entry.id)
        except BarcodeBuddyError as exc:
            logger.warning("Could not clean up Barcode Buddy for %s: %s", barcode, exc)

    logger.info("Single-barcode discover complete for EAN %s.", barcode)
    return {
        "success": True,
        "product": {
            "name": product.name,
            "barcode": product.ean,
            "description": product.description or "",
        },
        "grocy_id": grocy_id,
        "already_existed": not added,
    }


def _discover_products(args: argparse.Namespace) -> tuple[int, list[int]]:
    """Discover products via Barcode Buddy pending barcodes.

    Processes both "New Barcodes" (looked-up but unassigned) and "Unknown
    Barcodes" (not resolved at all) from Barcode Buddy.

    For each barcode:

    1. If Barcode Buddy already has a product name (New Barcode), use it
       directly; otherwise search K-Ruoka by EAN.
    2. Create the product in Grocy (via ``sync_product``).
    3. Add units to Grocy stock (using the quantity from BB).
    4. Remove the barcode from Barcode Buddy.

    Returns a ``(return_code, product_ids)`` tuple.  *return_code* is 0 on
    success and 1 if any errors occurred.  *product_ids* contains the Grocy
    IDs of all products that were successfully created or stocked during this
    run.
    """
    bbuddy = BarcodeBuddyClient(
        base_url=args.bbuddy_url,
        api_key=args.bbuddy_key,
        username=args.bbuddy_user,
        password=args.bbuddy_password,
    )
    grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
    store_ids = _parse_store_ids(args.store)
    scrapers = [
        KRuokaScraper(store_id=sid, use_graphql=args.use_graphql)
        for sid in store_ids
    ]

    # Pre-load known barcodes.
    known_barcodes: set[str] = set()
    try:
        for entry in grocy.get_all_barcodes():
            bc = entry.get("barcode")
            if bc:
                known_barcodes.add(str(bc))
        logger.info("  %d barcode(s) already registered in Grocy.", len(known_barcodes))
    except GrocyAPIError as exc:
        logger.warning("Could not fetch existing barcodes: %s", exc)

    # Fetch pending barcodes (new + unknown) from Barcode Buddy.
    try:
        pending = bbuddy.get_pending_barcodes()
    except BarcodeBuddyError as exc:
        logger.error("Failed to fetch barcodes from Barcode Buddy: %s", exc)
        return 1, []

    if not pending:
        logger.info("No pending barcodes in Barcode Buddy.")
        return 0, []

    logger.info("Found %d pending barcode(s) in Barcode Buddy.", len(pending))

    created = skipped = errors = 0
    discovered_ids: list[int] = []

    for entry in pending:
        barcode = entry.barcode
        bb_name = entry.name  # Non-empty for "New Barcodes", empty for unknown.
        logger.info("Looking up EAN %s …", barcode)

        # Always search K-Ruoka first; its data takes priority.
        # Fall back to S-kaupat, then the BB-resolved name.
        # Try each configured store until a match is found.
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

        if product is None and bb_name:
            logger.info("  Not on K-Ruoka, S-kaupat, or SearXNG; using Barcode Buddy name '%s'.", bb_name)
            product = Product(name=bb_name, ean=barcode)

        if product is None:
            logger.info("  EAN %s not found on K-Ruoka, S-kaupat, or SearXNG – skipping.", barcode)
            skipped += 1
            continue

        logger.info("  Found: '%s' (EAN %s).", product.name, product.ean)

        # Sync product to Grocy.
        try:
            added = sync_product(
                product,
                grocy,
                location_id=args.location_id,
                quantity_unit_id=args.quantity_unit_id,
                skip_existing=False,
                known_barcodes=known_barcodes,
                upload_images=args.upload_images,
            )
        except GrocyAPIError as exc:
            logger.error("Grocy error for '%s': %s", product.name, exc)
            errors += 1
            continue

        if not added:
            # Product already exists — look up its Grocy ID for stocking.
            existing = grocy.get_product_by_barcode(barcode)
            if existing:
                grocy_id = existing.get("id")
            else:
                logger.info("  Product already in Grocy – skipping stock/BB removal.")
                skipped += 1
                continue
        else:
            # Newly created — get the Grocy product ID via barcode lookup.
            existing = grocy.get_product_by_barcode(barcode)
            grocy_id = existing.get("id") if existing else None

        # Add to Grocy stock.
        if grocy_id is not None:
            discovered_ids.append(int(grocy_id))
            try:
                amount = float(entry.amount) if entry.amount else 1.0
                grocy.add_stock(int(grocy_id), amount=amount)
                logger.info(
                    "  → Added %.0f unit(s) to Grocy stock (product ID %s).",
                    amount, grocy_id,
                )
            except (GrocyAPIError, ValueError) as exc:
                logger.warning("  Could not add stock for '%s': %s", product.name, exc)

        # Remove from Barcode Buddy.
        try:
            bbuddy.delete_barcode(entry.id)
            logger.info("  → Removed EAN %s from Barcode Buddy.", barcode)
        except BarcodeBuddyError as exc:
            logger.warning(
                "  Could not remove EAN %s from Barcode Buddy: %s", barcode, exc
            )

        created += 1

    logger.info(
        "--discover complete: created/stocked: %d  not found: %d  errors: %d",
        created, skipped, errors,
    )
    return (0 if errors == 0 else 1), discovered_ids


def _delete_all_products(grocy: GrocyClient) -> int:
    """Delete every product from the Grocy database.

    Returns 0 on success, 1 if any errors occurred.
    """
    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Failed to fetch products from Grocy: %s", exc)
        return 1

    if not products:
        logger.info("No products in Grocy – nothing to delete.")
        return 0

    logger.info("Deleting %d product(s) from Grocy …", len(products))
    errors = 0
    for product in products:
        pid = product.get("id")
        name = product.get("name", "?")
        picture = product.get("picture_file_name", "")

        # Delete the product image first (if any).
        if picture:
            try:
                grocy.delete_product_image(picture)
                logger.debug("  Deleted image '%s' for product %s.", picture, pid)
            except GrocyAPIError as exc:
                logger.debug("  Could not delete image for product %s: %s", pid, exc)

        try:
            grocy.delete_product(int(pid))
            logger.debug("  Deleted product %s ('%s').", pid, name)
        except GrocyAPIError as exc:
            logger.error("  Failed to delete product %s ('%s'): %s", pid, name, exc)
            errors += 1

    deleted = len(products) - errors
    logger.info("Deleted %d product(s), %d error(s).", deleted, errors)
    return 0 if errors == 0 else 1


def _update_products(args: argparse.Namespace) -> int:
    """Update existing Grocy products with names and images from K-Ruoka / S-kaupat.

    For each product in Grocy that has at least one barcode, search K-Ruoka
    by EAN.  If not found, try S-kaupat.  When a match is found, update the
    product name (and optionally description) and upload the product image.

    Returns 0 on success, 1 if any errors occurred.
    """
    grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
    store_ids = _parse_store_ids(args.store)
    scrapers = [
        KRuokaScraper(store_id=sid, use_graphql=args.use_graphql)
        for sid in store_ids
    ]

    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Failed to fetch products from Grocy: %s", exc)
        return 1

    try:
        barcodes = grocy.get_all_barcodes()
    except GrocyAPIError as exc:
        logger.error("Failed to fetch barcodes from Grocy: %s", exc)
        return 1

    # Build product_id → list of EANs mapping.
    pid_to_eans: dict[int, list[str]] = {}
    for entry in barcodes:
        pid = entry.get("product_id")
        ean = entry.get("barcode")
        if pid is not None and ean:
            pid_to_eans.setdefault(int(pid), []).append(str(ean))

    if not products:
        logger.info("No products in Grocy – nothing to update.")
        return 0

    logger.info("Updating %d product(s) from K-Ruoka / S-kaupat …", len(products))
    updated = skipped = errors = 0
    max_products = getattr(args, "max_products", None)

    for grocy_product in products:
        if max_products is not None and updated >= max_products:
            logger.info("Reached --max-products limit (%d).", max_products)
            break

        pid = int(grocy_product["id"])
        current_name = grocy_product.get("name", "?")
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

        if found is None:
            logger.debug("  Product %d ('%s') not found online – skipping.", pid, current_name)
            skipped += 1
            continue

        # Update product in Grocy.
        update_fields: dict = {}
        if found.name and found.name != current_name:
            update_fields["name"] = found.name
        if found.description:
            update_fields["description"] = found.description

        if update_fields:
            try:
                grocy.update_product(pid, **update_fields)
                new_name = update_fields.get("name", current_name)
                logger.info(
                    "  Updated product %d: '%s' → '%s'.", pid, current_name, new_name
                )
            except GrocyAPIError as exc:
                logger.error("  Failed to update product %d ('%s'): %s", pid, current_name, exc)
                errors += 1
                continue
        else:
            logger.debug("  Product %d ('%s') already up to date.", pid, current_name)

        # Upload image if available.
        if found.image_url and args.upload_images:
            _upload_product_image(found, grocy, pid)

        updated += 1

    logger.info(
        "--update complete: updated: %d  skipped: %d  errors: %d",
        updated, skipped, errors,
    )
    return 0 if errors == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if _validate_args(args) != 0:
        return 1

    # AI analysis modes operate on the existing Grocy database independently
    # of the scraping pipeline.
    if args.sort or args.date or args.group or args.optimize:
        grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
        if args.optimize:
            _ai_optimize_products(
                grocy,
                args.gemini_api_key,
                args.gemini_model,
                location_id=getattr(args, "location_id", None),
                quantity_unit_id=getattr(args, "quantity_unit_id", None),
            )
        if args.sort:
            _ai_sort_products(grocy, args.gemini_api_key, args.gemini_model)
        if args.date:
            _ai_assign_due_dates(grocy, args.gemini_api_key, args.gemini_model)
        if args.group:
            _ai_group_products(
                grocy,
                args.gemini_api_key,
                args.gemini_model,
                location_id=getattr(args, "location_id", None),
                quantity_unit_id=getattr(args, "quantity_unit_id", None),
            )
        # If no scraping or discover mode was also requested, we are done.
        if not args.query and not args.browse and not args.discover:
            return 0

    # Discover mode: Barcode Buddy → K-Ruoka → Grocy pipeline.
    if args.discover:
        rc, discovered_ids = _discover_products(args)
        # After discover, run AI optimize when a Gemini key is available.
        if rc == 0 and args.gemini_api_key and discovered_ids:
            grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
            _ai_optimize_products(
                grocy,
                args.gemini_api_key,
                args.gemini_model,
                location_id=getattr(args, "location_id", None),
                quantity_unit_id=getattr(args, "quantity_unit_id", None),
                product_ids=discovered_ids,
            )
        return rc

    # Delete-all mode: wipe all products from Grocy.
    if args.delete_all:
        grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
        return _delete_all_products(grocy)

    # Update mode: refresh product names/images from online sources.
    if args.update:
        return _update_products(args)

    grocy, known_barcodes = _setup_grocy(args)
    return _process_products(args, grocy, known_barcodes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Interrupted – exiting.")
        sys.exit(130)
