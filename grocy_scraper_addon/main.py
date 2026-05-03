"""Main entry point for the grocy_scraper CLI.

Usage examples
--------------
Search for a specific product (GraphQL backend, no setup needed)::

    python main.py --store N110 --query "maito" \\
        --storage-url https://storage.example.com

Browse the full catalogue (GraphQL backend, no setup needed)::

    python main.py --store N110 --browse \\
        --storage-url https://storage.example.com

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

from grocy_scraper.storage_client import StorageAPIError, StorageClient
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

    # Scraping options
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
            "Fetch pending barcodes from the Storage barcode queue, search "
            "K-Ruoka for matching products, add them to Grocy, stock them, "
            "and mark the queue items as done."
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

    # Storage options
    parser.add_argument(
        "--storage-url",
        default=os.environ.get("STORAGE_URL", ""),
        dest="storage_url",
        metavar="URL",
        help=(
            "Base URL of the Storage instance (e.g. https://storage.example.com).  "
            "Also read from the STORAGE_URL environment variable."
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

    parser.add_argument(
        "--gemini-api-key",
        default=os.environ.get("GEMINI_API", ""),
        metavar="KEY",
        help=(
            "Gemini API key used for scraping-time AI helpers (package-size "
            "and density-conversion detection).  "
            "Also read from the GEMINI_API environment variable."
        ),
    )
    parser.add_argument(
        "--gemini-model",
        default=os.environ.get("GEMINI_MODEL", _GEMINI_DEFAULT_MODEL),
        metavar="MODEL",
        help=(
            "Gemini model name to use for scraping-time AI helpers "
            f"(default: {_GEMINI_DEFAULT_MODEL}).  "
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
    grocy: StorageClient,
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
    except StorageAPIError as exc:
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
            unit_id=quantity_unit_id,
        )
        logger.info("Created product '%s' (Grocy ID %d).", product.name, grocy_id)
    except StorageAPIError as exc:
        logger.error("Failed to create product '%s': %s", product.name, exc)
        return False

    # Attach the EAN barcode.
    try:
        grocy.add_barcode(grocy_id, product.ean)
        known_barcodes.add(product.ean)
        logger.info("  → Added barcode %s.", product.ean)
    except StorageAPIError as exc:
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


def _upload_product_image(product: Product, grocy: StorageClient, grocy_id: int) -> None:
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


# ---------------------------------------------------------------------------
# Quantity unit management
# ---------------------------------------------------------------------------

# Standard recipe units with Finnish names — kept in sync with HA-grocy-recipes
_STANDARD_UNITS = [
    {"name": "Gramma", "name_plural": "Grammaa", "abbreviation": "g"},
    {"name": "Kilogramma", "name_plural": "Kilogrammaa", "abbreviation": "kg"},
    {"name": "Millilitra", "name_plural": "Millilitraa", "abbreviation": "ml"},
    {"name": "Desilitra", "name_plural": "Desilitraa", "abbreviation": "dl"},
    {"name": "Litra", "name_plural": "Litraa", "abbreviation": "l"},
    {"name": "Teelusikka", "name_plural": "Teelusikkaa", "abbreviation": "tl"},
    {"name": "Ruokalusikka", "name_plural": "Ruokalusikkaa", "abbreviation": "rkl"},
    {"name": "Ripaus", "name_plural": "Ripausta", "abbreviation": "rs"},
    {"name": "Kappale", "name_plural": "Kappaletta", "abbreviation": "kpl"},
]

# Global conversions: (from_abbrev, to_abbrev, factor)  "1 <from> = <factor> <to>"
_GLOBAL_CONVERSIONS = [
    ("kg", "g", 1000),
    ("l", "dl", 10),
    ("l", "ml", 1000),
    ("dl", "ml", 100),
    ("rkl", "ml", 15),
    ("tl", "ml", 5),
]

# Map common unit string variations to canonical abbreviation
_UNIT_ALIASES: dict[str, str] = {
    "g": "g", "gr": "g", "gram": "g", "gramma": "g", "grammaa": "g",
    "kg": "kg", "kilo": "kg", "kilogramma": "kg", "kilogrammaa": "kg",
    "ml": "ml", "millilitra": "ml", "millilitraa": "ml",
    "dl": "dl", "desilitra": "dl", "desilitraa": "dl",
    "l": "l", "litra": "l", "litraa": "l",
    "tl": "tl", "teelusikka": "tl", "teelusikkaa": "tl",
    "rkl": "rkl", "ruokalusikka": "rkl", "ruokalusikkaa": "rkl",
    "rs": "rs", "ripaus": "rs", "ripausta": "rs",
    "kpl": "kpl", "kappale": "kpl", "kappaletta": "kpl",
    "pcs": "kpl", "piece": "kpl", "pieces": "kpl", "st": "kpl",
    "stück": "kpl", "pack": "kpl",
}

# Canonical abbreviations grouped by measurement domain
_WEIGHT_UNITS = {"g", "kg"}
_VOLUME_UNITS = {"ml", "dl", "l", "tl", "rkl"}


def _canonical_unit(name: str) -> str | None:
    """Normalise a unit name/description to its canonical abbreviation."""
    return _UNIT_ALIASES.get(name.lower().strip())


# ---------------------------------------------------------------------------
# AI provider globals (set by ingress_server before any AI call)
# ---------------------------------------------------------------------------

AI_PROVIDER: str = "gemini"  # "gemini" | "ollama" | "claude"
OLLAMA_URL: str = ""
OLLAMA_MODEL: str = "llama3"
CLAUDE_API_KEY: str = ""
CLAUDE_MODEL: str = "claude-3-5-haiku-20241022"

# ---------------------------------------------------------------------------
# Gemini AI helpers
# ---------------------------------------------------------------------------

_GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
)
_GEMINI_DEFAULT_MODEL = "gemini-1.5-flash"
_GEMINI_BATCH_SIZE = 100
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
        # Log token usage when available
        usage = data.get("usageMetadata", {})
        if usage:
            logger.info(
                "Gemini usage — prompt tokens: %s, output tokens: %s, total: %s",
                usage.get("promptTokenCount", "?"),
                usage.get("candidatesTokenCount", "?"),
                usage.get("totalTokenCount", "?"),
            )
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except requests.HTTPError as exc:
        raise StorageAPIError(f"Gemini API error: {exc}") from exc
    except requests.RequestException as exc:
        raise StorageAPIError(f"Gemini request failed: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise StorageAPIError(f"Unexpected Gemini response format: {exc}") from exc


def _call_ollama(prompt: str) -> str:
    """Send *prompt* to the Ollama chat API and return the text response."""
    if not OLLAMA_URL:
        raise StorageAPIError("Ollama URL is not configured")
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "format": "json",
                "stream": False,
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        # Log token/timing usage
        prompt_tokens = data.get("prompt_eval_count", "?")
        output_tokens = data.get("eval_count", "?")
        total_ns = data.get("total_duration")
        total_ms = round(total_ns / 1_000_000) if total_ns else "?"
        logger.info(
            "Ollama usage — prompt tokens: %s, output tokens: %s, total duration: %sms",
            prompt_tokens, output_tokens, total_ms,
        )
        return data["message"]["content"]
    except requests.HTTPError as exc:
        raise StorageAPIError(f"Ollama API error: {exc}") from exc
    except requests.RequestException as exc:
        raise StorageAPIError(f"Ollama request failed: {exc}") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise StorageAPIError(f"Unexpected Ollama response format: {exc}") from exc


def _extract_json_text(text: str) -> str:
    """Extract the JSON portion from an AI response that may include prose or markdown fences."""
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        return fence.group(1)
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if match:
        return match.group(1)
    return text.strip()


def _call_claude(prompt: str) -> str:
    """Send *prompt* to the Anthropic Claude API and return the text response."""
    if not CLAUDE_API_KEY:
        raise StorageAPIError("Claude API key is not configured")
    try:
        import anthropic as _anthropic

        client = _anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL or "claude-3-5-haiku-20241022",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = response.usage
        logger.info(
            "Claude usage — input tokens: %s, output tokens: %s",
            usage.input_tokens, usage.output_tokens,
        )
        return response.content[0].text
    except Exception as exc:  # anthropic raises its own exception hierarchy
        raise StorageAPIError(f"Claude API error: {exc}") from exc


def _call_gemini_json(
    prompt: str,
    api_key: str,
    model: str = _GEMINI_DEFAULT_MODEL,
    *,
    max_retries: int = _GEMINI_MAX_RETRIES,
) -> dict:
    """Call the configured AI provider, sanitize the response, and parse as JSON.

    When ``AI_PROVIDER == "ollama"``, ``api_key`` and ``model`` are ignored and
    Ollama's chat API is used instead of Gemini.  When ``AI_PROVIDER == "claude"``,
    the global ``CLAUDE_API_KEY`` / ``CLAUDE_MODEL`` are used instead.
    Retries up to *max_retries* times with exponential back-off on transient errors.
    """
    max_retries = max(max_retries, 1)
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            if AI_PROVIDER == "ollama":
                raw = _call_ollama(prompt)
            elif AI_PROVIDER == "claude":
                raw = _call_claude(prompt)
            else:
                raw = _call_gemini(prompt, api_key, model)
            # Strip control characters (except common whitespace) that
            # AI models occasionally embed in their output.
            sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
            # Extract JSON from Claude/Ollama responses that may include prose or fences
            if AI_PROVIDER in ("claude", "ollama"):
                sanitized = _extract_json_text(sanitized)
            return json.loads(sanitized)
        except (StorageAPIError, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = 2 ** attempt
                logger.warning(
                    "AI attempt %d/%d failed (%s), retrying in %ds …",
                    attempt,
                    max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unit optimization helpers
# ---------------------------------------------------------------------------


def _ensure_units_and_conversions(grocy: StorageClient) -> dict[str, int]:
    """Ensure standard recipe units and global conversions exist in Storage.

    Returns a mapping of canonical abbreviation → Storage QU ID.
    Idempotent — skips units/conversions that already exist.
    """
    existing_units = grocy.get_quantity_units()
    existing_by_desc: dict[str, int] = {}
    existing_by_name: dict[str, int] = {}
    for u in existing_units:
        if u.get("abbreviation"):
            existing_by_desc[u["abbreviation"].lower().strip()] = int(u["id"])
        if u.get("name"):
            existing_by_name[u["name"].lower().strip()] = int(u["id"])

    abbrev_to_id: dict[str, int] = {}

    for unit_def in _STANDARD_UNITS:
        abbrev = unit_def["abbreviation"]
        uid = existing_by_desc.get(abbrev.lower())
        if uid is None:
            uid = existing_by_name.get(unit_def["name"].lower())
        if uid is None:
            try:
                uid = grocy.create_quantity_unit(
                    unit_def["name"], unit_def["abbreviation"], unit_def["name_plural"],
                )
                logger.info("Created QU '%s' (ID %d).", unit_def["name"], uid)
            except StorageAPIError as exc:
                logger.warning("Failed to create QU '%s': %s", unit_def["name"], exc)
                continue
        abbrev_to_id[abbrev] = uid

    # Also map the built-in "Piece"/"Pack" defaults if present
    for u in existing_units:
        name_lower = (u.get("name") or "").lower().strip()
        if name_lower in ("piece", "pack", "stück"):
            abbrev_to_id.setdefault("piece", int(u["id"]))

    # Create global conversions
    existing_conversions = grocy.get_quantity_unit_conversions()
    conv_set: set[tuple[int, int]] = set()
    for c in existing_conversions:
        if c.get("product_id") is None or c.get("product_id") == "":
            conv_set.add((int(c["from_unit_id"]), int(c["to_unit_id"])))

    for from_abbrev, to_abbrev, factor in _GLOBAL_CONVERSIONS:
        from_id = abbrev_to_id.get(from_abbrev)
        to_id = abbrev_to_id.get(to_abbrev)
        if from_id is None or to_id is None:
            continue
        if (from_id, to_id) in conv_set:
            continue
        try:
            grocy.create_quantity_unit_conversion(from_id, to_id, factor)
            logger.info(
                "Created global conversion: 1 %s = %s %s", from_abbrev, factor, to_abbrev,
            )
        except StorageAPIError as exc:
            logger.warning("Failed to create conversion %s→%s: %s", from_abbrev, to_abbrev, exc)

    logger.info(
        "Unit map: %s",
        ", ".join(f"{abbrev}={uid}" for abbrev, uid in sorted(abbrev_to_id.items())),
    )
    return abbrev_to_id


# Pattern to extract size info from Finnish product names
_SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(kg|g|l|dl|ml|cl)\b",
    re.IGNORECASE,
)


def _fix_broken_product_units(
    grocy: StorageClient,
    abbrev_to_id: dict[str, int],
) -> int:
    """Detect products with orphaned/empty ``unit_id`` and repair them.

    Products whose ``unit_id`` references a non-existent quantity unit — or
    is null/empty — are broken.  This function sets a smart default based on
    the product name:

    * Name contains a weight (e.g. ``500g``, ``2kg``) → ``g`` or ``kg``
    * Name contains a volume (e.g. ``1L``, ``2dl``) → ``l`` or ``dl``
    * Parent products with no size hint → inherit unit from children
    * Otherwise (packaged items) → ``kpl``

    Also cleans up orphaned product-specific QU conversions that reference
    deleted units.

    Returns the number of products fixed.
    """
    existing_units = grocy.get_quantity_units()
    valid_ids: set[int] = {int(u["id"]) for u in existing_units}

    products = grocy.get_all_products()
    kpl_id = abbrev_to_id.get("kpl")
    fixed = 0

    # Build reverse map: QU ID → abbreviation
    id_to_abbrev: dict[int, str] = {v: k for k, v in abbrev_to_id.items()}

    # Build child → parent and parent → children maps for unit inheritance
    children_by_parent: dict[int, list[dict]] = {}
    for p in products:
        ppid = p.get("parent_id")
        if ppid is not None and ppid != "" and ppid != 0:
            children_by_parent.setdefault(int(ppid), []).append(p)

    # --- Clean up orphaned product-specific conversions ---
    conversions = grocy.get_quantity_unit_conversions()
    orphaned_convs = [
        c for c in conversions
        if int(c["from_unit_id"]) not in valid_ids
        or int(c["to_unit_id"]) not in valid_ids
    ]
    if orphaned_convs:
        deleted_count = 0
        for conv in orphaned_convs:
            try:
                grocy.delete_quantity_unit_conversion(int(conv["id"]))
                deleted_count += 1
            except StorageAPIError:
                pass
        if deleted_count:
            logger.info(
                "Cleaned up %d orphaned QU conversion(s) referencing deleted units.",
                deleted_count,
            )

    # --- Fix products with broken/empty unit_id ---
    for prod in products:
        val = prod.get("unit_id")
        if val is not None and val != "" and val != 0 and int(val) in valid_ids:
            continue

        # Determine smart default from product name
        name = prod.get("name", "")
        pid = int(prod["id"])
        default_unit_id = kpl_id  # fallback for packaged items
        default_label = "kpl"

        match = _SIZE_RE.search(name)
        if match:
            unit_str = match.group(2).lower()
            canonical = _canonical_unit(unit_str)
            if canonical and canonical in abbrev_to_id:
                default_unit_id = abbrev_to_id[canonical]
                default_label = canonical
        elif pid in children_by_parent:
            # Parent product with no size hint: inherit unit from children
            child_units: dict[int, int] = {}
            for child in children_by_parent[pid]:
                cval = child.get("unit_id")
                if cval is not None and cval != "" and int(cval) in valid_ids:
                    child_units[int(cval)] = child_units.get(int(cval), 0) + 1
            if child_units:
                best_id = max(child_units, key=child_units.get)
                if best_id != kpl_id or len(child_units) == 1:
                    default_unit_id = best_id
                    default_label = id_to_abbrev.get(best_id, str(best_id))

        if default_unit_id is None:
            continue

        try:
            grocy.update_product(pid, unit_id=default_unit_id)
            logger.info(
                "Fixed orphaned unit_id for '%s' (ID %d): set to '%s'.",
                name, pid, default_label,
            )
            fixed += 1
        except StorageAPIError as exc:
            logger.warning(
                "Failed to fix orphaned unit_id for '%s' (ID %d): %s",
                name, pid, exc,
            )

    if fixed:
        logger.info("Fixed orphaned unit_id for %d product(s).", fixed)
    return fixed


def _ai_detect_package_sizes(
    grocy: StorageClient,
    products: list[dict],
    abbrev_to_id: dict[str, int],
    gemini_api_key: str,
    model: str,
) -> int:
    """Use Gemini AI to extract package sizes from product names and create
    product-specific Piece→unit conversions.

    Returns the number of conversions created.
    """
    # Find the Piece unit ID (the stock QU for most products)
    piece_id = abbrev_to_id.get("piece") or abbrev_to_id.get("kpl")
    if piece_id is None:
        # Fall back: find the most common unit_id
        qu_counts: dict[int, int] = {}
        for p in products:
            qid = p.get("unit_id")
            if qid is not None:
                qu_counts[int(qid)] = qu_counts.get(int(qid), 0) + 1
        if qu_counts:
            piece_id = max(qu_counts, key=qu_counts.get)  # type: ignore[arg-type]
    if piece_id is None:
        logger.warning("Cannot detect package sizes — no Piece unit found.")
        return 0

    # Skip products that already have product-specific conversions
    existing_conversions = grocy.get_quantity_unit_conversions()
    products_with_conv: set[int] = set()
    for c in existing_conversions:
        cpid = c.get("product_id")
        if cpid is not None and cpid != "":
            products_with_conv.add(int(cpid))

    # Skip products that already have conversions
    candidates = [
        p for p in products
        if int(p["id"]) not in products_with_conv
        and p.get("active", True)
    ]
    if not candidates:
        logger.info("All products already have conversions — skipping package size detection.")
        return 0

    created = 0
    for i in range(0, len(candidates), _GEMINI_BATCH_SIZE):
        batch = candidates[i:i + _GEMINI_BATCH_SIZE]
        product_list = json.dumps(
            [{"product_id": int(p["id"]), "name": p.get("name", "")} for p in batch],
            ensure_ascii=False,
        )

        prompt = f"""Analyse these Finnish grocery product names and determine the package size for each.

Products:
{product_list}

For each product, determine:
1. The quantity in the package (e.g. "Arla Kevytmaito 1L" → amount: 1, unit: "l")
2. The unit of measurement (g, kg, ml, dl, l)

Return a JSON array:
[{{"product_id": <id>, "amount": <number>, "unit": "g"|"kg"|"ml"|"dl"|"l"|null}}]

RULES:
- Look for size indicators in the product name (e.g. "1L", "500g", "2kg", "200ml")
- Finnish products commonly use: g, kg, dl, l, ml
- If the name contains NO size information, return unit: null
- Common Finnish package sizes: milk 1L, flour 2kg, butter 500g, cream 2dl
- "tölkki" / "tlk" usually means a can (330ml for drinks, 400ml/400g for canned goods)
- Be precise — "500g" means amount: 500, unit: "g" — NOT amount: 0.5, unit: "kg"
- If multiple sizes appear, use the LAST/most specific one"""

        try:
            result = _call_gemini_json(prompt, gemini_api_key, model)
        except (StorageAPIError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("AI package size batch %d failed: %s", i // _GEMINI_BATCH_SIZE + 1, exc)
            continue

        if not isinstance(result, list):
            continue

        for item in result:
            pid = item.get("product_id")
            amount = item.get("amount")
            unit_abbrev = item.get("unit")
            if pid is None or amount is None or unit_abbrev is None:
                continue

            to_unit_id = abbrev_to_id.get(unit_abbrev)
            if to_unit_id is None:
                continue

            try:
                grocy.create_quantity_unit_conversion(
                    piece_id, to_unit_id, float(amount), product_id=int(pid),
                )
                logger.info(
                    "Created conversion for product %d: 1 piece = %s %s",
                    pid, amount, unit_abbrev,
                )
                created += 1
            except StorageAPIError as exc:
                logger.warning("Failed to create conversion for product %d: %s", pid, exc)

    logger.info("Package size detection: %d conversion(s) created.", created)
    return created


# Weight↔volume conversion factors relative to kg and l
_WEIGHT_FACTORS = {"kg": 1.0, "g": 1000.0}  # 1 kg = 1000 g
_VOLUME_FACTORS = {"l": 1.0, "dl": 10.0, "ml": 1000.0}  # 1 l = 10 dl = 1000 ml


def _derive_density_conversions(
    from_unit: str, to_unit: str, factor: float,
) -> list[tuple[str, str, float]]:
    """Compute all cross-domain weight↔volume pairs from one primary density conversion.

    Given e.g. ``("kg", "l", 1.67)`` → produces pairs like
    ``("kg", "dl", 16.7)``, ``("g", "l", 0.00167)``, etc.
    Excludes the primary pair itself.
    """
    # Determine which is weight and which is volume
    if from_unit in _WEIGHT_FACTORS and to_unit in _VOLUME_FACTORS:
        w_unit, v_unit = from_unit, to_unit
        # factor means: 1 <w_unit> = <factor> <v_unit>
        # Normalise to: 1 kg = X l
        kg_to_l = factor * _VOLUME_FACTORS[v_unit] / _WEIGHT_FACTORS[w_unit]
        # e.g., if 1 kg = 1.67 l → kg_to_l = 1.67
        # if 1 g = 0.001 l → kg_to_l = 0.001 * 1 / (1/1000) = 1.0... wait
        # Let's be precise: factor = w_factor_from_kg * kg_to_l / v_factor_from_l
        # 1 kg = 1.67 l → 1 g = 1.67/1000 l
        # normalise: 1 kg = factor * (VOLUME_FACTORS[v_unit]) / (WEIGHT_FACTORS[w_unit]) ... no
        # Actually: 1 w_unit = factor v_units
        # 1 kg = W_FACTORS[w_unit] w_units → 1 w_unit = 1/W[w] kg
        # factor v_units = factor/V[v] l
        # So 1/W[w] kg = factor/V[v] l → 1 kg = factor * W[w] / V[v] l
        kg_to_l = factor * _WEIGHT_FACTORS[w_unit] / _VOLUME_FACTORS[v_unit]
    elif from_unit in _VOLUME_FACTORS and to_unit in _WEIGHT_FACTORS:
        v_unit, w_unit = from_unit, to_unit
        # factor means: 1 <v_unit> = <factor> <w_unit>
        # Normalise: 1 l = X kg → 1 kg = 1/X l → kg_to_l = V[v] / (factor * W[w])
        kg_to_l = _VOLUME_FACTORS[v_unit] / (factor * _WEIGHT_FACTORS[w_unit])
    else:
        return []

    derived: list[tuple[str, str, float]] = []
    for w, w_f in _WEIGHT_FACTORS.items():
        for v, v_f in _VOLUME_FACTORS.items():
            if w == from_unit and v == to_unit:
                continue  # skip the primary pair
            # 1 w = (kg_to_l * v_f / w_f) v
            d_factor = round(kg_to_l * v_f / w_f, 6)
            if d_factor > 0:
                derived.append((w, v, d_factor))
    return derived


def _ai_detect_density_conversions(
    grocy: StorageClient,
    products: list[dict],
    abbrev_to_id: dict[str, int],
    gemini_api_key: str,
    model: str,
) -> int:
    """Use Gemini AI to determine weight↔volume density factors for products.

    For products that have a package-size conversion in one domain (weight or
    volume), ask Gemini for the approximate density so we can create a cross-
    domain conversion. E.g. 1 kg flour ≈ 1.67 L → create kg↔l conversion.

    Returns the number of conversions created.
    """
    # Gather per-product conversions to see what domain each product has
    conversions = grocy.get_quantity_unit_conversions()
    id_to_abbrev: dict[int, str] = {v: k for k, v in abbrev_to_id.items()}

    product_conv_units: dict[int, set[str]] = {}
    for c in conversions:
        cpid = c.get("product_id")
        if cpid is None or cpid == "":
            continue
        pid = int(cpid)
        for qu_field in ("from_unit_id", "to_unit_id"):
            qid = int(c[qu_field])
            abbrev = id_to_abbrev.get(qid)
            if abbrev:
                product_conv_units.setdefault(pid, set()).add(abbrev)

    # Find products that have weight but no volume, or volume but no weight
    prod_by_id = {int(p["id"]): p for p in products}
    need_density: list[dict] = []
    for pid, units in product_conv_units.items():
        has_weight = bool(units & _WEIGHT_UNITS)
        has_volume = bool(units & _VOLUME_UNITS)
        if has_weight and not has_volume or has_volume and not has_weight:
            prod = prod_by_id.get(pid)
            if prod:
                domain = "weight" if has_weight else "volume"
                need_density.append({
                    "product_id": pid,
                    "name": prod.get("name", ""),
                    "has_domain": domain,
                })

    if not need_density:
        logger.info("No products need cross-domain density conversions.")
        return 0

    created = 0
    for i in range(0, len(need_density), _GEMINI_BATCH_SIZE):
        batch = need_density[i:i + _GEMINI_BATCH_SIZE]
        product_list = json.dumps(batch, ensure_ascii=False)

        prompt = f"""For each Finnish grocery product below, estimate the density conversion
between weight and volume units. Products already have a size in one domain
(weight or volume) — provide the conversion to the OTHER domain.

Products:
{product_list}

Return a JSON array:
[{{"product_id": <id>, "from_unit": "kg"|"g"|"l"|"dl"|"ml", "to_unit": "kg"|"g"|"l"|"dl"|"ml", "factor": <number>}}]

RULES:
- For products with weight, provide a volume equivalent (e.g. 1 kg flour → factor: 1.67, from_unit: "kg", to_unit: "l")
- For products with volume, provide a weight equivalent (e.g. 1 l milk → factor: 1.03, from_unit: "l", to_unit: "kg")
- Use common grocery densities:
  - Milk/cream/juice: ~1.03 kg/l
  - Flour (vehnäjauho): ~0.6 kg/l (1 kg ≈ 1.67 l)
  - Sugar (sokeri): ~0.85 kg/l
  - Rice (riisi): ~0.85 kg/l
  - Oil (öljy): ~0.92 kg/l
  - Butter (voi): ~0.91 kg/l (911 g/l)
  - Honey (hunaja): ~1.4 kg/l
  - Salt (suola): ~1.2 kg/l
- If you cannot reasonably estimate the density, return null for factor
- Use the SIMPLEST conversion (prefer kg↔l over g↔ml)"""

        try:
            result = _call_gemini_json(prompt, gemini_api_key, model)
        except (StorageAPIError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("AI density batch %d failed: %s", i // _GEMINI_BATCH_SIZE + 1, exc)
            continue

        if not isinstance(result, list):
            continue

        for item in result:
            pid = item.get("product_id")
            factor = item.get("factor")
            from_unit = item.get("from_unit")
            to_unit = item.get("to_unit")
            if pid is None or factor is None or from_unit is None or to_unit is None:
                continue

            from_id = abbrev_to_id.get(from_unit)
            to_id = abbrev_to_id.get(to_unit)
            if from_id is None or to_id is None:
                continue

            try:
                grocy.create_quantity_unit_conversion(
                    from_id, to_id, float(factor), product_id=int(pid),
                )
                logger.info(
                    "Created density conversion for product %d: 1 %s = %s %s",
                    pid, from_unit, factor, to_unit,
                )
                created += 1
            except StorageAPIError as exc:
                logger.warning("Failed to create density conversion for product %d: %s", pid, exc)
                continue

            # Create derived cross-domain conversions so Grocy can resolve
            # recipe units without needing to chain product + global conversions.
            derived = _derive_density_conversions(from_unit, to_unit, float(factor))
            for d_from, d_to, d_factor in derived:
                d_from_id = abbrev_to_id.get(d_from)
                d_to_id = abbrev_to_id.get(d_to)
                if d_from_id is None or d_to_id is None:
                    continue
                try:
                    grocy.create_quantity_unit_conversion(
                        d_from_id, d_to_id, d_factor, product_id=int(pid),
                    )
                    created += 1
                except StorageAPIError:
                    pass  # likely already exists

    # Propagate density conversions from parent products to their children.
    # Grocy does NOT inherit product-specific conversions from parents.
    if created:
        all_convs = grocy.get_quantity_unit_conversions()
        children_of: dict[int, list[int]] = {}
        all_products = grocy.get_all_products()
        all_by_id = {int(p["id"]): p for p in all_products}
        for p in all_products:
            ppid = p.get("parent_id")
            if ppid:
                children_of.setdefault(int(ppid), []).append(int(p["id"]))
        processed_pids = {item["product_id"] for item in need_density}
        for pid in processed_pids:
            child_ids = children_of.get(pid, [])
            if not child_ids:
                continue
            parent_density = [
                c for c in all_convs
                if c.get("product_id") is not None
                and c["product_id"] != ""
                and int(c["product_id"]) == pid
                and id_to_abbrev.get(int(c["from_unit_id"])) in (_WEIGHT_UNITS | _VOLUME_UNITS)
                and id_to_abbrev.get(int(c["to_unit_id"])) in (_WEIGHT_UNITS | _VOLUME_UNITS)
            ]
            if not parent_density:
                continue
            for cid in child_ids:
                child_existing = {
                    (int(c["from_unit_id"]), int(c["to_unit_id"]))
                    for c in all_convs
                    if c.get("product_id") is not None
                    and c["product_id"] != ""
                    and int(c["product_id"]) == cid
                }
                propagated = 0
                for pc in parent_density:
                    pair = (int(pc["from_unit_id"]), int(pc["to_unit_id"]))
                    if pair in child_existing:
                        continue
                    try:
                        grocy.create_quantity_unit_conversion(
                            pair[0], pair[1], float(pc["factor"]), product_id=cid,
                        )
                        created += 1
                        propagated += 1
                    except StorageAPIError:
                        pass
                if propagated:
                    child_name = all_by_id.get(cid, {}).get("name", str(cid))
                    logger.info("Propagated %d density conversion(s) to child product %d (%s).",
                                propagated, cid, child_name)

    logger.info("Density conversion detection: %d conversion(s) created.", created)
    return created


def _fix_recipe_units(
    grocy: StorageClient,
    abbrev_to_id: dict[str, int],
    gemini_api_key: str | None = None,
    model: str | None = None,
) -> int:
    """Validate recipe ingredient units and fix missing conversions.

    For each recipe ingredient (``recipes_pos``), checks that its ``unit_id``
    can be converted to the product's ``unit_id``.  If not:

    * If both are standard units in the same domain (weight/volume),
      the global conversion already handles it — this is a no-op.
    * If the units are cross-domain (weight vs volume), attempt to create
      a density conversion via Gemini AI before falling back.
    * If the ingredient uses a measurable unit but the product's stock QU
      is ``kpl`` (piece), we need a product-specific conversion.  If one
      already exists from the package-size detection step, great.  If not,
      fall back to updating the recipe ingredient to use the product's
      stock QU so the recipe at least renders.
    * If the ingredient's QU ID doesn't exist at all, fall back to the
      product's stock QU.

    Returns the number of recipe positions fixed.
    """
    try:
        positions = grocy.get_recipe_positions()
    except StorageAPIError as exc:
        logger.warning("Could not fetch recipe positions: %s", exc)
        return 0

    if not positions:
        return 0

    products = grocy.get_all_products()
    prod_by_id: dict[int, dict] = {int(p["id"]): p for p in products}

    existing_units = grocy.get_quantity_units()
    valid_qu_ids: set[int] = {int(u["id"]) for u in existing_units}

    # Build conversion graph: (product_id_or_None, from_qu, to_qu) exists
    conversions = grocy.get_quantity_unit_conversions()
    conv_set: set[tuple[int | None, int, int]] = set()
    for c in conversions:
        cpid = c.get("product_id")
        cpid_val = int(cpid) if cpid is not None and cpid != "" else None
        from_id = int(c["from_unit_id"])
        to_id = int(c["to_unit_id"])
        # Store both global and product-specific
        conv_set.add((cpid_val, from_id, to_id))
        conv_set.add((cpid_val, to_id, from_id))  # bidirectional

    id_to_abbrev: dict[int, str] = {v: k for k, v in abbrev_to_id.items()}

    # Collect products needing density conversions (cross-domain gaps)
    density_candidates: list[dict] = []
    density_candidate_pids: set[int] = set()

    fixed = 0
    fallback_positions: list[tuple[dict, dict, int]] = []  # (pos, prod, stock_qu)

    for pos in positions:
        unit_id = pos.get("unit_id")
        pid = pos.get("product_id")
        if unit_id is None or pid is None:
            continue
        unit_id = int(unit_id)
        pid = int(pid)

        prod = prod_by_id.get(pid)
        if prod is None:
            continue

        stock_qu = prod.get("unit_id")
        if stock_qu is None:
            continue
        stock_qu = int(stock_qu)

        # Same unit — no conversion needed
        if unit_id == stock_qu:
            continue

        # Check if unit_id is even valid
        if unit_id not in valid_qu_ids:
            try:
                grocy.update_recipe_position(int(pos["id"]), unit_id=stock_qu)
                logger.info(
                    "Recipe pos %s: QU %d no longer exists, set to product stock QU '%s' (%d).",
                    pos["id"], unit_id,
                    id_to_abbrev.get(stock_qu, str(stock_qu)), stock_qu,
                )
                fixed += 1
            except StorageAPIError as exc:
                logger.warning("Failed to fix recipe pos %s: %s", pos["id"], exc)
            continue

        # Check if a conversion path exists (global or product-specific)
        has_conversion = (
            (None, unit_id, stock_qu) in conv_set
            or (pid, unit_id, stock_qu) in conv_set
        )
        if has_conversion:
            continue

        qu_abbrev = id_to_abbrev.get(unit_id)
        stock_abbrev = id_to_abbrev.get(stock_qu)

        # If both are in the same domain (weight↔weight or volume↔volume),
        # global conversions should chain.  Grocy handles this internally.
        if qu_abbrev and stock_abbrev:
            same_domain = (
                (qu_abbrev in _WEIGHT_UNITS and stock_abbrev in _WEIGHT_UNITS)
                or (qu_abbrev in _VOLUME_UNITS and stock_abbrev in _VOLUME_UNITS)
            )
            if same_domain:
                continue

        # Cross-domain gap — collect for density creation
        if qu_abbrev and stock_abbrev:
            is_cross_domain = (
                (qu_abbrev in _WEIGHT_UNITS and stock_abbrev in _VOLUME_UNITS)
                or (qu_abbrev in _VOLUME_UNITS and stock_abbrev in _WEIGHT_UNITS)
            )
            if is_cross_domain and pid not in density_candidate_pids:
                existing_domain = "weight" if stock_abbrev in _WEIGHT_UNITS else "volume"
                density_candidates.append({
                    "product_id": pid,
                    "name": prod.get("name", ""),
                    "has_domain": existing_domain,
                })
                density_candidate_pids.add(pid)
                # Defer the fallback — try density creation first
                fallback_positions.append((pos, prod, stock_qu))
                continue

        # Also check if recipe unit is weight/volume but stock is kpl/piece
        # These need a density→piece chain; collect for density if product
        # has package-size conversions in one domain only
        if qu_abbrev and (qu_abbrev in _WEIGHT_UNITS or qu_abbrev in _VOLUME_UNITS):
            # Check what domains the product already has
            prod_conv_abbrevs: set[str] = set()
            for c in conversions:
                cpid = c.get("product_id")
                if cpid is not None and cpid != "" and int(cpid) == pid:
                    for qf in ("from_unit_id", "to_unit_id"):
                        a = id_to_abbrev.get(int(c[qf]))
                        if a:
                            prod_conv_abbrevs.add(a)
            has_w = bool(prod_conv_abbrevs & _WEIGHT_UNITS)
            has_v = bool(prod_conv_abbrevs & _VOLUME_UNITS)
            if (has_w and not has_v and qu_abbrev in _VOLUME_UNITS) or \
               (has_v and not has_w and qu_abbrev in _WEIGHT_UNITS):
                if pid not in density_candidate_pids:
                    existing_domain = "weight" if has_w else "volume"
                    density_candidates.append({
                        "product_id": pid,
                        "name": prod.get("name", ""),
                        "has_domain": existing_domain,
                    })
                    density_candidate_pids.add(pid)
                fallback_positions.append((pos, prod, stock_qu))
                continue

        # No conversion path — fall back to product's stock QU
        try:
            grocy.update_recipe_position(int(pos["id"]), unit_id=stock_qu)
            logger.info(
                "Recipe pos %s (product '%s'): no conversion from '%s' to '%s', set to stock QU.",
                pos["id"],
                prod.get("name", pid),
                id_to_abbrev.get(unit_id, str(unit_id)),
                id_to_abbrev.get(stock_qu, str(stock_qu)),
            )
            fixed += 1
        except StorageAPIError as exc:
            logger.warning("Failed to fix recipe pos %s: %s", pos["id"], exc)

    # Attempt density creation for cross-domain gaps
    if density_candidates and gemini_api_key and model:
        density_products = [
            prod_by_id[d["product_id"]]
            for d in density_candidates
            if d["product_id"] in prod_by_id
        ]
        if density_products:
            created = _ai_detect_density_conversions(
                grocy, density_products, abbrev_to_id,
                gemini_api_key, model,
            )
            if created:
                logger.info(
                    "Created %d density conversion(s) to fix recipe unit gaps.", created,
                )
                # Re-check: refresh conversion set and see if fallbacks are still needed
                conversions = grocy.get_quantity_unit_conversions()
                conv_set = set()
                for c in conversions:
                    cpid = c.get("product_id")
                    cpid_val = int(cpid) if cpid is not None and cpid != "" else None
                    from_id = int(c["from_unit_id"])
                    to_id = int(c["to_unit_id"])
                    conv_set.add((cpid_val, from_id, to_id))
                    conv_set.add((cpid_val, to_id, from_id))

    # Process deferred fallback positions
    for pos, prod, stock_qu in fallback_positions:
        unit_id = int(pos["unit_id"])
        pid = int(pos["product_id"])
        # Re-check if density creation resolved it
        has_conversion = (
            (None, unit_id, stock_qu) in conv_set
            or (pid, unit_id, stock_qu) in conv_set
        )
        if has_conversion:
            continue
        # Still no path — fall back to stock QU
        try:
            grocy.update_recipe_position(int(pos["id"]), unit_id=stock_qu)
            logger.info(
                "Recipe pos %s (product '%s'): no conversion from '%s' to '%s', set to stock QU.",
                pos["id"],
                prod.get("name", pid),
                id_to_abbrev.get(unit_id, str(unit_id)),
                id_to_abbrev.get(stock_qu, str(stock_qu)),
            )
            fixed += 1
        except StorageAPIError as exc:
            logger.warning("Failed to fix recipe pos %s: %s", pos["id"], exc)

    if fixed:
        logger.info("Fixed %d recipe position(s) with invalid units.", fixed)
    return fixed


def _merge_recipe_stubs(grocy: StorageClient) -> int:
    """Merge recipe-created stub products into matching parent products.

    When the recipe scraper creates a stub (e.g. "suola") and a later barcode
    scan creates a real parent product (e.g. "Suola"), we need to:
    1. Move all recipe positions from the stub to the parent
    2. Delete the stub product

    Returns the number of stubs merged.
    """
    products = grocy.get_all_products()
    prod_by_id = {int(p["id"]): p for p in products}

    # Identify stubs: products with "Auto-created by recipe scraper" description
    stubs: list[dict] = []
    for p in products:
        desc = (p.get("description") or "").strip()
        if "Auto-created by recipe scraper" in desc:
            stubs.append(p)

    if not stubs:
        return 0

    # Build case-insensitive name→product map for non-stub products
    name_to_product: dict[str, list[dict]] = {}
    for p in products:
        desc = (p.get("description") or "").strip()
        if "Auto-created by recipe scraper" in desc:
            continue
        name_to_product.setdefault(p["name"].lower().strip(), []).append(p)

    merged = 0
    positions = None  # lazy-load

    for stub in stubs:
        stub_name = stub["name"].lower().strip()
        candidates = name_to_product.get(stub_name, [])
        if not candidates:
            continue

        # Prefer a parent product (one that has children)
        parent_ids = {
            int(p["parent_id"])
            for p in products
            if p.get("parent_id")
        }
        target = None
        for c in candidates:
            if c["id"] in parent_ids:
                target = c
                break
        if target is None:
            target = candidates[0]

        stub_id = int(stub["id"])
        target_id = int(target["id"])

        # Move recipe positions from stub to target
        if positions is None:
            try:
                positions = grocy.get_recipe_positions()
            except StorageAPIError:
                positions = []

        moved = 0
        for pos in positions:
            if pos.get("product_id") is not None and int(pos["product_id"]) == stub_id:
                try:
                    grocy.update_recipe_position(int(pos["id"]), product_id=target_id)
                    moved += 1
                except StorageAPIError as exc:
                    logger.warning(
                        "Failed to move recipe position %d from stub '%s' to '%s': %s",
                        pos["id"], stub["name"], target["name"], exc,
                    )

        # Delete the stub product
        try:
            grocy.delete_product(stub_id)
            logger.info(
                "Merged stub '%s' (ID %d) → '%s' (ID %d): %d recipe position(s) moved.",
                stub["name"], stub_id, target["name"], target_id, moved,
            )
            merged += 1
        except StorageAPIError as exc:
            logger.warning(
                "Failed to delete stub product '%s' (ID %d): %s",
                stub["name"], stub_id, exc,
            )

    if merged:
        logger.info("Stub merge: %d stub product(s) merged.", merged)
    return merged


def _check_recipes_for_unit_gaps(
    grocy: StorageClient,
    product_ids: set[int],
    abbrev_to_id: dict[str, int],
    gemini_api_key: str,
    model: str,
) -> int:
    """Check existing recipes for unit gaps with specific products.

    After a product is discovered/optimized, scan existing recipes to see if
    any ingredients reference the product with a unit that lacks a conversion
    path. If a cross-domain gap is found, trigger density conversion creation.

    Returns the number of conversions created.
    """
    try:
        positions = grocy.get_recipe_positions()
    except StorageAPIError as exc:
        logger.warning("Could not fetch recipe positions for gap check: %s", exc)
        return 0

    if not positions:
        return 0

    # Filter to positions referencing the target products
    relevant = [
        p for p in positions
        if p.get("product_id") is not None and int(p["product_id"]) in product_ids
    ]
    if not relevant:
        return 0

    products = grocy.get_all_products()
    prod_by_id: dict[int, dict] = {int(p["id"]): p for p in products}
    id_to_abbrev: dict[int, str] = {v: k for k, v in abbrev_to_id.items()}

    conversions = grocy.get_quantity_unit_conversions()
    product_conv_units: dict[int, set[str]] = {}
    for c in conversions:
        cpid = c.get("product_id")
        if cpid is None or cpid == "":
            continue
        pid = int(cpid)
        for qu_field in ("from_unit_id", "to_unit_id"):
            abbrev = id_to_abbrev.get(int(c[qu_field]))
            if abbrev:
                product_conv_units.setdefault(pid, set()).add(abbrev)

    # Find products with cross-domain gaps relative to recipe units
    need_density: list[dict] = []
    seen: set[int] = set()

    for pos in relevant:
        pid = int(pos["product_id"])
        unit_id = pos.get("unit_id")
        if pid in seen or unit_id is None:
            continue

        recipe_abbrev = id_to_abbrev.get(int(unit_id))
        if not recipe_abbrev or recipe_abbrev == "kpl":
            continue

        if recipe_abbrev in _WEIGHT_UNITS:
            recipe_domain = "weight"
        elif recipe_abbrev in _VOLUME_UNITS:
            recipe_domain = "volume"
        else:
            continue

        prod_units = product_conv_units.get(pid, set())
        has_weight = bool(prod_units & _WEIGHT_UNITS)
        has_volume = bool(prod_units & _VOLUME_UNITS)

        if has_weight and has_volume:
            continue  # already cross-domain

        if recipe_domain == "volume" and has_weight and not has_volume:
            existing_domain = "weight"
        elif recipe_domain == "weight" and has_volume and not has_weight:
            existing_domain = "volume"
        else:
            continue

        prod = prod_by_id.get(pid)
        if prod:
            need_density.append({
                "product_id": pid,
                "name": prod.get("name", ""),
                "has_domain": existing_domain,
            })
            seen.add(pid)

    if not need_density:
        return 0

    logger.info(
        "Found %d product(s) with cross-domain unit gaps in recipes.",
        len(need_density),
    )
    return _ai_detect_density_conversions(
        grocy,
        [prod_by_id[d["product_id"]] for d in need_density if d["product_id"] in prod_by_id],
        abbrev_to_id,
        gemini_api_key,
        model,
    )


def _optimize_units(
    grocy: StorageClient,
    gemini_api_key: str,
    model: str,
    products: list[dict] | None = None,
) -> int:
    """Run the full unit optimization pipeline.

    1. Ensure standard units and global conversions exist.
    2. Fix products with orphaned (non-existent) QU IDs.
    3. AI-detect package sizes and create Piece→unit conversions.
    4. AI-detect density factors for cross-domain conversions.
    5. Fix recipe ingredients with invalid unit conversions.

    Returns the total number of conversions created.
    """
    logger.info("--- Unit optimization ---")

    # Step 1: Ensure standard units
    try:
        abbrev_to_id = _ensure_units_and_conversions(grocy)
    except StorageAPIError as exc:
        logger.error("Failed to ensure standard units: %s", exc)
        return 0

    # Step 2: Fix products with orphaned/empty QU IDs.
    try:
        _fix_broken_product_units(grocy, abbrev_to_id)
    except StorageAPIError as exc:
        logger.warning("Failed to fix broken product units: %s", exc)

    # Step 3: Fetch products if not provided
    if products is None:
        try:
            products = grocy.get_all_products()
        except StorageAPIError as exc:
            logger.error("Failed to fetch products for unit optimization: %s", exc)
            return 0

    if not products:
        logger.info("No products found — skipping unit optimization.")
        return 0

    # Step 4: AI package size detection
    pkg_count = _ai_detect_package_sizes(
        grocy, products, abbrev_to_id, gemini_api_key, model,
    )

    # Step 5: AI density conversions
    density_count = _ai_detect_density_conversions(
        grocy, products, abbrev_to_id, gemini_api_key, model,
    )

    # Step 6: Fix recipe ingredient units (with density creation for gaps)
    try:
        _fix_recipe_units(grocy, abbrev_to_id, gemini_api_key, model)
    except StorageAPIError as exc:
        logger.warning("Failed to fix recipe units: %s", exc)

    total = pkg_count + density_count
    logger.info("--- Unit optimization complete: %d conversion(s) created. ---", total)
    return total



def _validate_args(args: argparse.Namespace) -> int:
    """Return 0 if arguments are valid, 1 otherwise."""
    scrape_mode = bool(args.query or args.browse)
    discover_mode = args.discover
    delete_all_mode = args.delete_all
    update_mode = args.update

    # At least one operational mode must be selected.
    if not scrape_mode and not discover_mode and not delete_all_mode and not update_mode:
        logger.error(
            "Specify a scraping mode (--query / --browse), --discover, "
            "--update, or --delete-all."
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

    if (scrape_mode and not args.dry_run) or discover_mode or delete_all_mode or update_mode:
        # Storage connection is required for non-dry-run scraping, discover,
        # update, and delete-all.
        if not args.storage_url:
            logger.error(
                "Storage URL is required.  Use --storage-url or set STORAGE_URL."
            )
            return 1

    return 0


def wait_for_storage(base_url: str, max_retries: int = 30, delay: float = 5.0) -> None:
    """Block until Storage addon is reachable."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(f"{base_url}/api/health", timeout=5)
            if resp.ok:
                logger.info("Storage addon is ready (%s).", resp.json().get("version", "?"))
                return
        except requests.RequestException:
            pass
        if attempt < max_retries:
            logger.info("Storage not ready (attempt %d/%d), retrying in %.0fs…", attempt, max_retries, delay)
            time.sleep(delay)
    raise SystemExit("ERROR: Storage addon not reachable after %d attempts." % max_retries)


def _setup_grocy(args: argparse.Namespace) -> tuple[StorageClient | None, set[str]]:
    """Create a Grocy client and pre-load known barcodes if not a dry run."""
    if args.dry_run:
        return None, set()

    grocy = StorageClient(base_url=args.storage_url)
    known_barcodes: set[str] = set()

    if args.skip_existing:
        logger.info("Fetching existing barcodes from Grocy …")
        try:
            for entry in grocy.get_all_barcodes():
                bc = entry.get("barcode")
                if bc:
                    known_barcodes.add(str(bc))
            logger.info("  %d barcode(s) already registered.", len(known_barcodes))
        except StorageAPIError as exc:
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


def _process_products(args: argparse.Namespace, grocy: StorageClient | None, known_barcodes: set[str]) -> int:
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
        except StorageAPIError as exc:
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

    The caller already knows the barcode.  It searches online stores, creates
    the product in Grocy, adds 1 unit to stock, and returns a result dict.

    Returns ``{"success": True, "product": {...}, "grocy_id": int}`` on
    success, or ``{"success": False, "error": "..."}`` on failure.
    """
    grocy = StorageClient(base_url=args.storage_url)
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
    except StorageAPIError as exc:
        logger.error("Grocy error for '%s': %s", product.name, exc)
        return {"success": False, "error": f"Failed to create product in Grocy: {exc}"}

    # Look up the Grocy product ID.
    grocy_id = None
    try:
        existing = grocy.get_product_by_barcode(barcode)
        if existing:
            grocy_id = existing.get("id")
    except StorageAPIError:
        pass

    # Add 1 unit to stock.
    if grocy_id is not None:
        try:
            grocy.add_stock(int(grocy_id), amount=1.0)
            logger.info("Added 1 unit to Grocy stock (product ID %s).", grocy_id)
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
        "grocy_id": grocy_id,
        "already_existed": not added,
    }


def _discover_products(args: argparse.Namespace) -> tuple[int, list[int]]:
    """Discover products via the Storage barcode queue.

    Fetches pending items from the barcode queue, searches online stores for
    each barcode, creates/stocks them in Grocy, and marks queue items as done.

    For each barcode:

    1. Search K-Ruoka by EAN; fall back to S-kaupat, SearXNG.
    2. Create the product in Grocy (via ``sync_product``).
    3. Add 1 unit to Grocy stock.
    4. Mark the barcode queue item as done with the resulting product ID.

    Returns a ``(return_code, product_ids)`` tuple.  *return_code* is 0 on
    success and 1 if any errors occurred.  *product_ids* contains the Grocy
    IDs of all products that were successfully created or stocked during this
    run.
    """
    grocy = StorageClient(base_url=args.storage_url)
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
    except StorageAPIError as exc:
        logger.warning("Could not fetch existing barcodes: %s", exc)

    # Fetch pending barcodes from the Storage barcode queue.
    try:
        pending = grocy.get_barcode_queue(status="pending")
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
                    grocy.update_barcode_queue_item(
                        queue_id, status="error",
                        error_message=f"Product not found for EAN {barcode}",
                    )
                except StorageAPIError:
                    pass
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
        except StorageAPIError as exc:
            logger.error("Grocy error for '%s': %s", product.name, exc)
            if queue_id is not None:
                try:
                    grocy.update_barcode_queue_item(
                        queue_id, status="error", error_message=str(exc),
                    )
                except StorageAPIError:
                    pass
            errors += 1
            continue

        if not added:
            # Product already exists — look up its Grocy ID for stocking.
            existing = grocy.get_product_by_barcode(barcode)
            if existing:
                grocy_id = existing.get("id")
            else:
                logger.info("  Product already in Grocy – skipping.")
                skipped += 1
                continue
        else:
            # Newly created — get the Grocy product ID via barcode lookup.
            existing = grocy.get_product_by_barcode(barcode)
            grocy_id = existing.get("id") if existing else None

        # Add to Grocy stock.
        if grocy_id is not None:
            discovered_ids.append(int(grocy_id))
            raw_stock = entry.get("import_stock_amount")
            is_grocy_import = entry.get("source") == "grocy-import"
            if is_grocy_import and raw_stock is None:
                # Grocy import with NULL amount = product had no stock in Grocy — skip.
                logger.info(
                    "  → Skipping stock for product ID %s (not in Grocy stock).",
                    grocy_id,
                )
            else:
                stock_amount = float(raw_stock) if raw_stock is not None else 1.0
                try:
                    grocy.add_stock(int(grocy_id), amount=stock_amount)
                    logger.info(
                        "  → Added %g unit(s) to Grocy stock (product ID %s).",
                        stock_amount, grocy_id,
                    )
                except (StorageAPIError, ValueError) as exc:
                    logger.warning("  Could not add stock for '%s': %s", product.name, exc)

        # Mark the queue item as done.
        if queue_id is not None:
            try:
                grocy.update_barcode_queue_item(
                    queue_id, status="done",
                    result_product_id=int(grocy_id) if grocy_id else None,
                )
                logger.info("  → Marked queue item %s as done.", queue_id)
            except StorageAPIError as exc:
                logger.warning(
                    "  Could not update queue item %s: %s", queue_id, exc,
                )

        created += 1

    logger.info(
        "--discover complete: created/stocked: %d  not found: %d  errors: %d",
        created, skipped, errors,
    )
    return (0 if errors == 0 else 1), discovered_ids


def _delete_all_products(grocy: StorageClient) -> int:
    """Delete every product from the Grocy database.

    Returns 0 on success, 1 if any errors occurred.
    """
    try:
        products = grocy.get_all_products()
    except StorageAPIError as exc:
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
        picture = product.get("picture_filename", "")

        # Delete the product image file (CASCADE handles DB records).
        if picture:
            try:
                grocy.delete_product_image(picture)
                logger.debug("  Deleted image '%s' for product %s.", picture, pid)
            except StorageAPIError as exc:
                logger.debug("  Could not delete image for product %s: %s", pid, exc)

        try:
            grocy.delete_product(int(pid))
            logger.debug("  Deleted product %s ('%s').", pid, name)
        except StorageAPIError as exc:
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
    grocy = StorageClient(base_url=args.storage_url)
    store_ids = _parse_store_ids(args.store)
    scrapers = [
        KRuokaScraper(store_id=sid, use_graphql=args.use_graphql)
        for sid in store_ids
    ]

    try:
        products = grocy.get_all_products()
    except StorageAPIError as exc:
        logger.error("Failed to fetch products from Grocy: %s", exc)
        return 1

    try:
        barcodes = grocy.get_all_barcodes()
    except StorageAPIError as exc:
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
            except StorageAPIError as exc:
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

    if not args.dry_run:
        wait_for_storage(args.storage_url)

    # Discover mode: barcode queue → K-Ruoka → Grocy pipeline.
    # AI categorisation is owned by HA-Storage now; callers are expected
    # to POST to /api/ai/optimize themselves with the discovered ids.
    if args.discover:
        rc, _discovered_ids = _discover_products(args)
        return rc

    # Delete-all mode: wipe all products from Grocy.
    if args.delete_all:
        grocy = StorageClient(base_url=args.storage_url)
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
