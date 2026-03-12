"""Main entry point for the grocy_scraper CLI.

Usage examples
--------------
Search for a specific product (GraphQL backend, no setup needed)::

    python main.py --store N110 --query "maito" \\
        --grocy-url https://grocy.example.com --grocy-key MY_API_KEY

Browse the full catalogue (GraphQL backend, no setup needed)::

    python main.py --store N110 --browse \\
        --grocy-url https://grocy.example.com --grocy-key MY_API_KEY

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
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; fall back to plain env vars

from grocy_scraper.barcodebuddy_client import BarcodeBuddyClient, BarcodeBuddyError
from grocy_scraper.grocy_client import GrocyAPIError, GrocyClient
from grocy_scraper.scraper import KRuokaScraper, Product
from grocy_scraper.skaupat_client import SKaupatError, lookup_ean as skaupat_lookup

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


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
        metavar="STORE_ID",
        help=(
            "K-group store ID (e.g. N110 = K-Supermarket Helsinki, "
            "N137 = K-Citymarket Tammisto).  "
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
        default=os.environ.get("GROCY_LOCATION_ID"),
        metavar="ID",
        help=(
            "Grocy location ID to assign to new products (required). "
            "Also read from the GROCY_LOCATION_ID environment variable."
        ),
    )
    parser.add_argument(
        "--quantity-unit-id",
        type=int,
        default=os.environ.get("GROCY_QUANTITY_UNIT_ID"),
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
        "--gemini-api-key",
        default=os.environ.get("GEMINI_API", ""),
        metavar="KEY",
        help=(
            "Gemini API key used for AI-powered --sort and --date analysis.  "
            "Also read from the GEMINI_API environment variable."
        ),
    )
    parser.add_argument(
        "--gemini-model",
        default=os.environ.get("GEMINI_MODEL", _GEMINI_DEFAULT_MODEL),
        metavar="MODEL",
        help=(
            "Gemini model name to use for --sort and --date analysis "
            f"(default: {_GEMINI_DEFAULT_MODEL}).  "
            "Also read from the GEMINI_MODEL environment variable."
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
            timeout=60,
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


def _ai_sort_products(grocy: GrocyClient, gemini_api_key: str, model: str = _GEMINI_DEFAULT_MODEL) -> int:
    """Use Gemini AI to assign each Grocy product to an appropriate location.

    Fetches all locations and products from Grocy, asks Gemini to map each
    product to a location, then updates the products in Grocy.

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
            "Consider: dairy / meat / fresh produce → refrigerator; "
            "cleaning / laundry supplies → cleaning cabinet; "
            "dry goods / canned / packaged → cupboard / pantry.\n\n"
            "Return ONLY a JSON object mapping product IDs (as strings) to "
            "location IDs (as integers), e.g. {\"1\": 2, \"5\": 4}.\n\n"
            "Products:\n"
            f"{product_lines}"
        )
        try:
            raw = _call_gemini(prompt, gemini_api_key, model)
            mapping: dict = json.loads(raw)
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

    logger.info("--sort complete: %d product(s) updated.", updated)
    return updated


def _ai_assign_due_dates(grocy: GrocyClient, gemini_api_key: str, model: str = _GEMINI_DEFAULT_MODEL) -> int:
    """Use Gemini AI to set default best-before days for each Grocy product.

    Fetches all products from Grocy, asks Gemini to estimate typical best-before
    days for each, then updates the products in Grocy.

    Returns the number of products updated.
    """
    try:
        products = grocy.get_all_products()
    except GrocyAPIError as exc:
        logger.error("Could not fetch products from Grocy: %s", exc)
        return 0

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
            raw = _call_gemini(prompt, gemini_api_key, model)
            mapping: dict = json.loads(raw)
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


def _validate_args(args: argparse.Namespace) -> int:
    """Return 0 if arguments are valid, 1 otherwise."""
    ai_mode = args.sort or args.date
    scrape_mode = bool(args.query or args.browse)
    discover_mode = args.discover
    delete_all_mode = args.delete_all
    update_mode = args.update

    # At least one operational mode must be selected.
    if not ai_mode and not scrape_mode and not discover_mode and not delete_all_mode and not update_mode:
        logger.error(
            "Specify a scraping mode (--query / --browse), an AI analysis mode "
            "(--sort / --date), --discover, --update, or --delete-all."
        )
        return 1

    # Store is required when scraping, discovering, or updating.
    if scrape_mode or discover_mode or update_mode:
        if not args.store:
            logger.error(
                "Store ID is required.  Use --store or set the KRUOKA_STORE_ID "
                "environment variable."
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
                "Gemini API key is required for --sort / --date.  "
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
    """Return an iterator of products from the k-ruoka.fi scraper."""
    scraper = KRuokaScraper(store_id=args.store, use_graphql=args.use_graphql)
    if args.query:
        backend = "GraphQL" if args.use_graphql else "kr-api"
        logger.info(
            "Searching k-ruoka.fi (store=%s, backend=%s) for '%s' …",
            args.store, backend, args.query,
        )
        return scraper.search(args.query, max_products=args.max_products)
    backend = "GraphQL" if args.use_graphql else "kr-api"
    logger.info(
        "Browsing k-ruoka.fi catalogue (store=%s, backend=%s) …",
        args.store, backend,
    )
    return scraper.browse(max_products=args.max_products)


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


def _discover_products(args: argparse.Namespace) -> int:
    """Discover products via Barcode Buddy pending barcodes.

    Processes both "New Barcodes" (looked-up but unassigned) and "Unknown
    Barcodes" (not resolved at all) from Barcode Buddy.

    For each barcode:

    1. If Barcode Buddy already has a product name (New Barcode), use it
       directly; otherwise search K-Ruoka by EAN.
    2. Create the product in Grocy (via ``sync_product``).
    3. Add units to Grocy stock (using the quantity from BB).
    4. Remove the barcode from Barcode Buddy.

    Returns 0 on success, 1 if any errors occurred.
    """
    bbuddy = BarcodeBuddyClient(
        base_url=args.bbuddy_url,
        api_key=args.bbuddy_key,
        username=args.bbuddy_user,
        password=args.bbuddy_password,
    )
    grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
    scraper = KRuokaScraper(store_id=args.store, use_graphql=args.use_graphql)

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
        return 1

    if not pending:
        logger.info("No pending barcodes in Barcode Buddy.")
        return 0

    logger.info("Found %d pending barcode(s) in Barcode Buddy.", len(pending))

    created = skipped = errors = 0

    for entry in pending:
        barcode = entry.barcode
        bb_name = entry.name  # Non-empty for "New Barcodes", empty for unknown.
        logger.info("Looking up EAN %s …", barcode)

        # Always search K-Ruoka first; its data takes priority.
        # Fall back to S-kaupat, then the BB-resolved name.
        product = None
        for p in scraper.search(barcode, max_products=10):
            if p.ean == barcode:
                product = p
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

        if product is None and bb_name:
            logger.info("  Not on K-Ruoka or S-kaupat; using Barcode Buddy name '%s'.", bb_name)
            product = Product(name=bb_name, ean=barcode)

        if product is None:
            logger.info("  EAN %s not found on K-Ruoka or S-kaupat – skipping.", barcode)
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
    return 0 if errors == 0 else 1


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
    scraper = KRuokaScraper(store_id=args.store, use_graphql=args.use_graphql)

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

        # Try each EAN until we find a match.
        found: Product | None = None
        matched_ean = ""
        for ean in eans:
            # K-Ruoka first.
            for p in scraper.search(ean, max_products=10):
                if p.ean == ean:
                    found = p
                    matched_ean = ean
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
    if args.sort or args.date:
        grocy = GrocyClient(base_url=args.grocy_url, api_key=args.grocy_key)
        if args.sort:
            _ai_sort_products(grocy, args.gemini_api_key, args.gemini_model)
        if args.date:
            _ai_assign_due_dates(grocy, args.gemini_api_key, args.gemini_model)
        # If no scraping or discover mode was also requested, we are done.
        if not args.query and not args.browse and not args.discover:
            return 0

    # Discover mode: Barcode Buddy → K-Ruoka → Grocy pipeline.
    if args.discover:
        return _discover_products(args)

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
