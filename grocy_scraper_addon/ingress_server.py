"""Interactive ingress web server for the Grocy Scraper add-on.

Serves a single-page application that lets users:

* Search for K-Ruoka products by keyword
* Run Discover, Sort, Date, Group, and Update operations via action buttons
* View console/log output in a terminal pane with verbose toggle

API endpoints
-------------
GET  /                  Serve the HTML UI
GET  /api/config        Return current add-on configuration summary
POST /api/search        Search products on K-Ruoka
POST /api/discover      Run Barcode Buddy -> K-Ruoka -> Grocy discover pipeline
POST /api/sort          Run Gemini AI product location sorting
POST /api/date          Run Gemini AI best-before date assignment
POST /api/group         Run Gemini AI product grouping (parent-product assignment)
POST /api/update        Update existing products from K-Ruoka
POST /api/add_products  Add selected products to the Grocy database
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Generator

_PORT = int(os.environ.get("INGRESS_PORT", "8099"))
_OPTIONS_PATH = "/data/options.json"

# Ensure the app directory is importable (main.py + grocy_scraper package).
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

# Serialise long-running operations so only one runs at a time.
_op_lock = threading.Lock()

logger = logging.getLogger(__name__)

# Logger namespaces whose records are captured for terminal output.
_CAPTURE_NAMESPACES = ("grocy_scraper", "main", "__main__")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _read_options() -> dict[str, Any]:
    """Read add-on options from ``/data/options.json``."""
    try:
        with open(_OPTIONS_PATH) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Log-capturing helpers (mirrors ws_api._CapturingHandler)
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    """Logging handler that stores records for returning to the frontend."""

    _FORMATTER = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(self._FORMATTER)
        self.records: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "level": record.levelname,
                "message": self.format(record),
            }
        )


@contextmanager
def _capture_logs() -> Generator[list[dict[str, str]], None, None]:
    """Temporarily attach a capturing handler to grocy_scraper loggers."""
    cap = _CapturingHandler()
    targets = [logging.getLogger(ns) for ns in _CAPTURE_NAMESPACES]
    saved: list[tuple[logging.Logger, int]] = []
    for lgr in targets:
        saved.append((lgr, lgr.level))
        lgr.addHandler(cap)
        if lgr.level == logging.NOTSET or lgr.level > logging.DEBUG:
            lgr.setLevel(logging.DEBUG)
    try:
        yield cap.records
    finally:
        for lgr, old_level in saved:
            lgr.removeHandler(cap)
            lgr.setLevel(old_level)


# ---------------------------------------------------------------------------
# Namespace builder
# ---------------------------------------------------------------------------


def _build_args(opts: dict[str, Any], **overrides: Any) -> argparse.Namespace:
    """Build an ``argparse.Namespace`` from add-on options."""
    ns = argparse.Namespace(
        store=opts.get("store_id", ""),
        grocy_url=opts.get("grocy_url", ""),
        grocy_key=opts.get("grocy_api_key", ""),
        location_id=opts.get("location_id", 0),
        quantity_unit_id=opts.get("quantity_unit_id", 0),
        bbuddy_url=opts.get("bbuddy_url", ""),
        bbuddy_key=opts.get("bbuddy_api_key", ""),
        bbuddy_user=opts.get("bbuddy_user", ""),
        bbuddy_password=opts.get("bbuddy_password", ""),
        upload_images=opts.get("upload_images", True),
        use_graphql=opts.get("use_graphql", True),
        gemini_api_key=opts.get("gemini_api_key", ""),
        gemini_model=opts.get("gemini_model", "gemini-1.5-flash"),
        verbose=False,
        dry_run=False,
        skip_existing=True,
        max_products=None,
    )
    for key, val in overrides.items():
        setattr(ns, key, val)
    return ns


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------


def _handle_config() -> dict[str, Any]:
    """Return a config summary for the settings badge area."""
    opts = _read_options()
    bbuddy_configured = bool(
        opts.get("bbuddy_url") and opts.get("bbuddy_user") and opts.get("bbuddy_password")
    )
    return {
        "configured": bool(opts.get("grocy_url")),
        "store_id": opts.get("store_id", ""),
        "discover_interval": opts.get("discover_interval", 60),
        "bbuddy_configured": bbuddy_configured,
        "gemini_configured": bool(opts.get("gemini_api_key")),
    }


def _handle_search(body: dict[str, Any]) -> dict[str, Any]:
    """Search products on K-Ruoka.

    When multiple store IDs are configured (comma-separated), each store is
    tried in order until one succeeds.
    """
    query = str(body.get("query", "")).strip()
    if not query:
        return {"success": False, "error": "Search query is required."}

    max_products = int(body.get("max_products", 50))
    opts = _read_options()

    try:
        from grocy_scraper.scraper import KRuokaScraper

        raw_store = opts.get("store_id", "")
        store_ids = [s.strip() for s in raw_store.split(",") if s.strip()] or [""]
        use_graphql = opts.get("use_graphql", True)

        last_exc: Exception | None = None
        for idx, store_id in enumerate(store_ids):
            try:
                scraper = KRuokaScraper(
                    store_id=store_id,
                    use_graphql=use_graphql,
                )
                products: list[dict[str, str]] = []
                for product in scraper.search(query, max_products=max_products):
                    products.append(
                        {
                            "name": product.name,
                            "ean": product.ean or "",
                            "description": product.description or "",
                            "image_url": getattr(product, "image_url", "") or "",
                        }
                    )
                return {"success": True, "products": products}
            except Exception as exc:
                last_exc = exc
                if idx < len(store_ids) - 1:
                    logger.warning(
                        "Store %s failed (%s); trying next store …", store_id, exc,
                    )

        # All stores failed — report the last error.
        raise last_exc  # type: ignore[misc]
    except Exception as exc:
        logger.exception("Product search failed")
        return {"success": False, "error": str(exc)}


def _handle_discover() -> dict[str, Any]:
    """Run the Barcode Buddy -> K-Ruoka -> Grocy discover pipeline.

    After discover succeeds, automatically runs AI sort, date and group
    when a Gemini API key is configured.
    """
    opts = _read_options()
    if not (opts.get("bbuddy_url") and opts.get("bbuddy_user") and opts.get("bbuddy_password")):
        return {
            "success": False,
            "skipped": True,
            "logs": [
                {
                    "level": "WARNING",
                    "message": "Barcode Buddy URL, username, and password are "
                    "required for Discover. Configure them in the add-on options.",
                }
            ],
        }

    from grocy_scraper.grocy_client import GrocyClient
    import main as _main

    args = _build_args(opts)
    with _capture_logs() as logs:
        result_code: int = _main._discover_products(args)
        # Chain AI sort/date/group when Gemini key is available.
        gemini_key = opts.get("gemini_api_key", "")
        if result_code == 0 and gemini_key:
            grocy = GrocyClient(
                base_url=opts.get("grocy_url", ""),
                api_key=opts.get("grocy_api_key", ""),
            )
            model = opts.get("gemini_model", "gemini-1.5-flash")
            _main._ai_sort_products(grocy, gemini_key, model)
            _main._ai_assign_due_dates(grocy, gemini_key, model)
            location_id = int(opts.get("location_id", 0)) or None
            quantity_unit_id = int(opts.get("quantity_unit_id", 0)) or None
            _main._ai_group_products(
                grocy,
                gemini_key,
                model,
                location_id=location_id,
                quantity_unit_id=quantity_unit_id,
            )
    return {"success": result_code == 0, "skipped": False, "logs": logs}


def _handle_sort() -> dict[str, Any]:
    """Run Gemini AI product-location sorting."""
    opts = _read_options()
    gemini_key = opts.get("gemini_api_key", "")
    if not gemini_key:
        return {
            "success": False,
            "skipped": True,
            "updated": 0,
            "logs": [
                {
                    "level": "WARNING",
                    "message": "A Gemini API key is required for Sort. "
                    "Add it in the add-on configuration.",
                }
            ],
        }

    from grocy_scraper.grocy_client import GrocyClient
    import main as _main

    grocy = GrocyClient(
        base_url=opts.get("grocy_url", ""),
        api_key=opts.get("grocy_api_key", ""),
    )
    model = opts.get("gemini_model", "gemini-1.5-flash")
    with _capture_logs() as logs:
        updated: int = _main._ai_sort_products(grocy, gemini_key, model)
    return {"success": True, "skipped": False, "updated": updated, "logs": logs}


def _handle_date() -> dict[str, Any]:
    """Run Gemini AI best-before-date assignment."""
    opts = _read_options()
    gemini_key = opts.get("gemini_api_key", "")
    if not gemini_key:
        return {
            "success": False,
            "skipped": True,
            "updated": 0,
            "logs": [
                {
                    "level": "WARNING",
                    "message": "A Gemini API key is required for Date. "
                    "Add it in the add-on configuration.",
                }
            ],
        }

    from grocy_scraper.grocy_client import GrocyClient
    import main as _main

    grocy = GrocyClient(
        base_url=opts.get("grocy_url", ""),
        api_key=opts.get("grocy_api_key", ""),
    )
    model = opts.get("gemini_model", "gemini-1.5-flash")
    with _capture_logs() as logs:
        updated: int = _main._ai_assign_due_dates(grocy, gemini_key, model)
    return {"success": True, "skipped": False, "updated": updated, "logs": logs}


def _handle_group() -> dict[str, Any]:
    """Run Gemini AI product grouping (parent-product assignment)."""
    opts = _read_options()
    gemini_key = opts.get("gemini_api_key", "")
    if not gemini_key:
        return {
            "success": False,
            "skipped": True,
            "updated": 0,
            "logs": [
                {
                    "level": "WARNING",
                    "message": "A Gemini API key is required for Group. "
                    "Add it in the add-on configuration.",
                }
            ],
        }

    from grocy_scraper.grocy_client import GrocyClient
    import main as _main

    grocy = GrocyClient(
        base_url=opts.get("grocy_url", ""),
        api_key=opts.get("grocy_api_key", ""),
    )
    model = opts.get("gemini_model", "gemini-1.5-flash")
    location_id = int(opts.get("location_id", 0)) or None
    quantity_unit_id = int(opts.get("quantity_unit_id", 0)) or None
    with _capture_logs() as logs:
        updated: int = _main._ai_group_products(
            grocy,
            gemini_key,
            model,
            location_id=location_id,
            quantity_unit_id=quantity_unit_id,
        )
    return {"success": True, "skipped": False, "updated": updated, "logs": logs}


def _handle_update() -> dict[str, Any]:
    """Update existing Grocy products from K-Ruoka / S-kaupat."""
    opts = _read_options()

    import main as _main

    args = _build_args(opts)
    with _capture_logs() as logs:
        result_code: int = _main._update_products(args)
    return {"success": result_code == 0, "logs": logs}


def _handle_add_products(body: dict[str, Any]) -> dict[str, Any]:
    """Add selected products to the Grocy database."""
    products = body.get("products")
    if not products or not isinstance(products, list):
        return {"success": False, "error": "No products provided."}

    opts = _read_options()
    grocy_url = opts.get("grocy_url", "")
    grocy_key = opts.get("grocy_api_key", "")
    if not grocy_url or not grocy_key:
        return {"success": False, "error": "Grocy URL and API key must be configured."}

    from grocy_scraper.grocy_client import GrocyClient, GrocyAPIError
    from grocy_scraper.scraper import Product

    import main as _main

    grocy = GrocyClient(base_url=grocy_url, api_key=grocy_key)
    location_id = int(opts.get("location_id", 0)) or None
    quantity_unit_id = int(opts.get("quantity_unit_id", 0)) or None
    upload_images = opts.get("upload_images", True)

    added = 0
    errors: list[str] = []
    with _capture_logs() as logs:
        for item in products:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            ean = str(item.get("ean", "")).strip()
            description = str(item.get("description", "")).strip()
            image_url = str(item.get("image_url", "")).strip()
            try:
                # Skip if a product with this barcode already exists.
                if ean and grocy.get_product_by_barcode(ean):
                    logger.info("Skipped '%s' – barcode %s already exists.", name, ean)
                    continue

                product_id = grocy.create_product(
                    name,
                    description=description,
                    location_id=location_id,
                    quantity_unit_id=quantity_unit_id,
                )
                if ean:
                    grocy.add_barcode(product_id, ean)
                if upload_images and image_url:
                    product = Product(
                        name=name, ean=ean, description=description,
                        image_url=image_url,
                    )
                    _main._upload_product_image(product, grocy, product_id)
                logger.info("Added '%s' (id=%d, ean=%s).", name, product_id, ean or "–")
                added += 1
            except GrocyAPIError as exc:
                msg = f"Failed to add '{name}': {exc}"
                logger.error(msg)
                errors.append(msg)
            except Exception as exc:
                msg = f"Unexpected error adding '{name}': {exc}"
                logger.exception(msg)
                errors.append(msg)

    return {
        "success": len(errors) == 0,
        "added": added,
        "errors": errors,
        "logs": logs,
    }


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Grocy Scraper</title>
  <style>
    /* ── Reset & base ── */
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: Roboto, 'Segoe UI', sans-serif;
      margin: 0; padding: 16px;
      background: #121212; color: #e0e0e0;
      min-height: 100vh;
    }

    /* ── Header ── */
    .header { display:flex; align-items:center; gap:12px; margin-bottom:20px; }
    .header h1 { font-size:1.5rem; font-weight:400; margin:0; color:#e0e0e0; }

    /* ── Cards ── */
    .card {
      background:#1e1e1e; border-radius:12px; padding:20px;
      box-shadow:0 2px 4px rgba(0,0,0,.4); margin-bottom:16px;
    }
    .card h2 { font-size:0.95rem; font-weight:500; margin:0 0 14px; color:#e0e0e0; }

    /* ── Forms ── */
    .form-row { display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap; }
    .form-group { display:flex; flex-direction:column; gap:5px; }
    .form-group.grow { flex:1; min-width:200px; }
    label { font-size:0.8rem; color:#9e9e9e; font-weight:500; letter-spacing:0.02em; }
    input[type="text"], input[type="number"] {
      padding:10px 14px; border:1px solid #444; border-radius:8px;
      background:#2a2a2a; color:#e0e0e0; font-size:0.95rem;
      outline:none; transition:border-color 0.2s;
    }
    input[type="text"]:focus, input[type="number"]:focus { border-color:#03a9f4; }
    input[type="number"] { width:90px; }

    /* ── Buttons ── */
    .btn {
      padding:10px 20px; border:none; border-radius:8px;
      font-size:0.9rem; cursor:pointer; transition:opacity 0.2s;
      white-space:nowrap; font-weight:500;
    }
    .btn:hover { opacity:0.88; }
    .btn:disabled { opacity:0.4; cursor:not-allowed; }
    .btn-primary { background:#03a9f4; color:#fff; }
    .btn-discover { background:#6200ea; color:#fff; }
    .btn-sort     { background:#00796b; color:#fff; }
    .btn-date     { background:#e65100; color:#fff; }
    .btn-update   { background:#1565c0; color:#fff; }
    .btn-group    { background:#6a1b9a; color:#fff; }
    .btn-sm {
      padding:5px 12px; font-size:0.78rem; border-radius:6px;
      background:#333; color:#bbb; border:1px solid #555;
    }

    /* ── Status ── */
    .status { margin-top:10px; min-height:20px; font-size:0.85rem; color:#9e9e9e; }
    .error   { color:#f44747; }
    .success { color:#4caf50; }

    /* ── Spinner ── */
    .loader {
      display:inline-block; width:14px; height:14px;
      border:2px solid #444; border-top-color:#03a9f4;
      border-radius:50%; animation:spin 0.7s linear infinite;
      vertical-align:middle; margin-right:6px;
    }
    @keyframes spin { to { transform:rotate(360deg); } }

    /* ── Results grid ── */
    .results {
      display:grid; grid-template-columns:repeat(auto-fill,minmax(270px,1fr));
      gap:12px; margin-bottom:16px;
    }
    .product-card {
      background:#1e1e1e; border-radius:12px; padding:14px;
      box-shadow:0 2px 4px rgba(0,0,0,.4);
      display:flex; gap:12px; align-items:flex-start;
    }
    .product-img {
      width:60px; height:60px; object-fit:contain; border-radius:8px;
      background:#2a2a2a; flex-shrink:0;
    }
    .product-placeholder {
      width:60px; height:60px; background:#2a2a2a; border-radius:8px;
      display:flex; align-items:center; justify-content:center;
      font-size:1.6rem; flex-shrink:0;
    }
    .product-info { flex:1; min-width:0; }
    .product-name {
      font-weight:500; color:#e0e0e0; margin-bottom:4px;
      word-break:break-word; line-height:1.3;
    }
    .product-ean {
      font-size:0.72rem; color:#9e9e9e;
      font-family:monospace; letter-spacing:0.05em;
    }
    .product-desc {
      font-size:0.78rem; color:#9e9e9e; margin-top:4px;
      display:-webkit-box; -webkit-line-clamp:2;
      -webkit-box-orient:vertical; overflow:hidden;
    }
    .no-results {
      text-align:center; color:#9e9e9e; padding:32px 16px;
      grid-column:1/-1; font-size:0.9rem;
    }

    /* ── Selection toolbar ── */
    .selection-toolbar {
      display:flex; gap:10px; align-items:center; flex-wrap:wrap;
      margin-bottom:12px;
    }
    .selection-toolbar.hidden { display:none; }
    .btn-select  { background:#424242; color:#e0e0e0; border:1px solid #555; }
    .btn-add     { background:#4caf50; color:#fff; }
    .selection-count { font-size:0.82rem; color:#9e9e9e; margin-left:auto; }

    /* ── Product checkbox ── */
    .product-card { position:relative; }
    .product-check {
      position:absolute; top:10px; right:10px;
      width:18px; height:18px; cursor:pointer;
      accent-color:#03a9f4;
    }

    /* ── Action row ── */
    .action-row { display:flex; gap:10px; flex-wrap:wrap; }

    /* ── Terminal ── */
    .terminal-header {
      display:flex; align-items:center; gap:10px;
      margin-bottom:10px; flex-wrap:wrap;
    }
    .terminal-header h2 { margin:0; flex:1; min-width:120px; }
    .verbose-label {
      display:inline-flex; align-items:center; gap:6px;
      font-size:0.82rem; color:#9e9e9e; cursor:pointer; user-select:none;
    }
    .verbose-label input[type="checkbox"] { cursor:pointer; }
    .terminal {
      background:#0d0d0d; color:#d4d4d4; border-radius:8px; padding:14px;
      font-family:'Courier New',Courier,monospace; font-size:0.78rem;
      line-height:1.55; overflow-y:auto; max-height:400px;
      white-space:pre-wrap; word-break:break-all;
      scrollbar-width:thin; scrollbar-color:#555 #0d0d0d;
    }
    .terminal:empty::before {
      content:"No output yet. Run an action above.";
      color:#555; font-style:italic;
    }
    .log-debug    { color:#858585; }
    .log-info     { color:#d4d4d4; }
    .log-warning  { color:#ffcc00; }
    .log-error    { color:#f44747; }
    .log-critical { color:#f44747; font-weight:bold; }
    .log-session-head {
      color:#569cd6; font-weight:bold; margin-top:6px;
      border-top:1px solid #333; padding-top:4px;
    }
    .terminal:not(.verbose) .log-debug { display:none; }

    /* ── Config badges ── */
    .config-info { font-size:0.85rem; color:#9e9e9e; display:flex; flex-wrap:wrap; gap:8px; }
    .badge {
      display:inline-flex; align-items:center; padding:3px 10px;
      border-radius:12px; background:#03a9f4; color:#fff;
      font-size:0.75rem; font-weight:600; gap:4px;
    }
    .badge.ok   { background:#4caf50; }
    .badge.warn { background:#ff9800; }
  </style>
</head>
<body>
  <!-- Header -->
  <div class="header"><h1>&#128722; Grocy Scraper</h1></div>

  <!-- Search card -->
  <div class="card">
    <h2>Search for products on K-Ruoka</h2>
    <div class="form-row">
      <div class="form-group grow">
        <label for="query">Search term</label>
        <input type="text" id="query"
          placeholder="e.g. maito, leip&auml;, juusto &hellip;" autocomplete="off" />
      </div>
      <div class="form-group">
        <label for="max-products">Max results</label>
        <input type="number" id="max-products" value="10" min="1" max="500" />
      </div>
      <button class="btn btn-primary" id="search-btn">Search</button>
    </div>
    <div class="status" id="search-status"></div>
  </div>

  <!-- Selection toolbar + search results -->
  <div class="selection-toolbar hidden" id="selection-toolbar">
    <button class="btn btn-sm btn-select" id="select-all-btn">Select All</button>
    <button class="btn btn-sm btn-select" id="select-none-btn">Select None</button>
    <button class="btn btn-sm btn-add" id="add-products-btn">&#10133; Add Products</button>
    <span class="selection-count" id="selection-count"></span>
  </div>
  <div class="status" id="add-status"></div>
  <div class="results" id="results"></div>

  <!-- Actions card -->
  <div class="card">
    <h2>&#128295; Actions</h2>
    <div class="action-row">
      <button class="btn btn-discover" id="discover-btn">&#128269; Discover</button>
      <button class="btn btn-sort"     id="sort-btn">&#128230; Sort</button>
      <button class="btn btn-date"     id="date-btn">&#128197; Date</button>
      <button class="btn btn-group"    id="group-btn">&#128279; Group</button>
      <button class="btn btn-update"   id="update-btn">&#128260; Update</button>
    </div>
    <div class="status" id="action-status"></div>
  </div>

  <!-- Terminal output card -->
  <div class="card" id="terminal-card">
    <div class="terminal-header">
      <h2>&#128223; Output</h2>
      <label class="verbose-label">
        <input type="checkbox" id="verbose-toggle" /> Verbose
      </label>
      <button class="btn btn-sm" id="clear-btn">Clear</button>
    </div>
    <div class="terminal" id="terminal"></div>
  </div>

  <!-- Config info card -->
  <div class="card" id="config-card" style="display:none">
    <h2>&#9881;&#65039; Add-on settings</h2>
    <div class="config-info" id="config-info"></div>
  </div>

<script>
(function () {
  "use strict";

  // ── State ─────────────────────────────────────────────────────────────────
  var searching = false;
  var running   = false;
  var verbose   = false;

  // ── DOM refs ──────────────────────────────────────────────────────────────
  var $ = function (sel) { return document.querySelector(sel); };
  var searchBtn   = $("#search-btn");
  var queryInput  = $("#query");
  var maxInput    = $("#max-products");
  var searchStat  = $("#search-status");
  var resultsDiv  = $("#results");
  var selToolbar  = $("#selection-toolbar");
  var selectAllBtn  = $("#select-all-btn");
  var selectNoneBtn = $("#select-none-btn");
  var addProductsBtn = $("#add-products-btn");
  var selectionCount = $("#selection-count");
  var addStatus   = $("#add-status");
  var discoverBtn = $("#discover-btn");
  var sortBtn     = $("#sort-btn");
  var dateBtn     = $("#date-btn");
  var groupBtn    = $("#group-btn");
  var updateBtn   = $("#update-btn");
  var actionStat  = $("#action-status");
  var terminal    = $("#terminal");
  var verboseChk  = $("#verbose-toggle");
  var clearBtn    = $("#clear-btn");
  var configCard  = $("#config-card");
  var configInfo  = $("#config-info");

  var actionBtns = [discoverBtn, sortBtn, dateBtn, groupBtn, updateBtn];
  var lastProducts = [];

  // ── Utilities ─────────────────────────────────────────────────────────────
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (resp) { return resp.json(); });
  }

  // ── Terminal helpers ──────────────────────────────────────────────────────
  function appendLogs(label, logs) {
    var now = new Date().toLocaleTimeString();
    var heading = document.createElement("div");
    heading.className = "log-session-head";
    heading.textContent = "\\u25b6 " + label + "  [" + now + "]";
    terminal.appendChild(heading);

    for (var i = 0; i < logs.length; i++) {
      var entry = logs[i];
      var line = document.createElement("div");
      var lvl = (entry.level || "INFO").toUpperCase();
      line.className = "log-" + lvl.toLowerCase();
      line.setAttribute("data-level", lvl);
      line.textContent = entry.message;
      terminal.appendChild(line);
    }
    terminal.classList.toggle("verbose", verbose);
    terminal.scrollTop = terminal.scrollHeight;
  }

  // ── Search ────────────────────────────────────────────────────────────────
  function doSearch() {
    if (searching) return;
    var q = queryInput.value.trim();
    if (!q) return;

    var max = parseInt(maxInput.value, 10) || 50;
    searching = true;
    searchBtn.disabled = true;
    searchStat.innerHTML = '<span class="loader"></span>Searching for "' + esc(q) + '" \\u2026';
    resultsDiv.innerHTML = "";

    api("POST", "api/search", { query: q, max_products: max })
      .then(function (data) {
        if (!data.success) {
          searchStat.innerHTML = '<span class="error">Error: ' + esc(data.error) + "</span>";
          return;
        }
        var products = data.products || [];
        searchStat.textContent = products.length > 0
          ? "Found " + products.length + " product(s)."
          : 'No products found for "' + q + '".';
        renderResults(products);
      })
      .catch(function (err) {
        searchStat.innerHTML = '<span class="error">Error: ' + esc(err) + "</span>";
      })
      .finally(function () {
        searching = false;
        searchBtn.disabled = false;
      });
  }

  function renderResults(products) {
    lastProducts = products;
    if (!products.length) {
      resultsDiv.innerHTML = '<div class="no-results">No products found. Try a different search term.</div>';
      selToolbar.classList.add("hidden");
      addStatus.innerHTML = "";
      return;
    }
    selToolbar.classList.remove("hidden");
    addStatus.innerHTML = "";
    resultsDiv.innerHTML = products.map(function (p, idx) {
      var img = p.image_url
        ? '<img class="product-img" src="' + esc(p.image_url) + '" alt="' + esc(p.name) + '" loading="lazy" />'
        : '<div class="product-placeholder">&#128230;</div>';
      var desc = p.description
        ? '<div class="product-desc">' + esc(p.description) + "</div>"
        : "";
      return '<div class="product-card">' +
        '<input type="checkbox" class="product-check" data-idx="' + idx + '" checked />' +
        img +
        '<div class="product-info">' +
        '<div class="product-name">' + esc(p.name) + "</div>" +
        '<div class="product-ean">' + esc(p.ean || "\\u2014") + "</div>" +
        desc + "</div></div>";
    }).join("");
    updateSelectionCount();
  }

  // ── Selection helpers ──────────────────────────────────────────────────────
  function getCheckboxes() {
    return resultsDiv.querySelectorAll(".product-check");
  }

  function updateSelectionCount() {
    var boxes = getCheckboxes();
    var checked = 0;
    for (var i = 0; i < boxes.length; i++) { if (boxes[i].checked) checked++; }
    selectionCount.textContent = checked + " of " + boxes.length + " selected";
    addProductsBtn.disabled = checked === 0;
  }

  function setAllChecked(val) {
    var boxes = getCheckboxes();
    for (var i = 0; i < boxes.length; i++) { boxes[i].checked = val; }
    updateSelectionCount();
  }

  // ── Add products ───────────────────────────────────────────────────────────
  function doAddProducts() {
    if (running) return;
    var boxes = getCheckboxes();
    var selected = [];
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) {
        selected.push(lastProducts[parseInt(boxes[i].getAttribute("data-idx"), 10)]);
      }
    }
    if (!selected.length) return;

    running = true;
    addProductsBtn.disabled = true;
    actionBtns.forEach(function (b) { b.disabled = true; });
    addStatus.innerHTML = '<span class="loader"></span>Adding ' + selected.length + ' product(s) \\u2026';

    api("POST", "api/add_products", { products: selected })
      .then(function (data) {
        if (data.logs && data.logs.length) appendLogs("Add Products", data.logs);
        if (data.success) {
          addStatus.innerHTML = '<span class="success">\\u2713 Added ' + data.added + ' product(s) to Grocy.</span>';
        } else {
          var msg = data.error || (data.errors && data.errors.length ? data.errors.join("; ") : "Unknown error");
          addStatus.innerHTML = '<span class="error">Error: ' + esc(msg) + "</span>";
        }
      })
      .catch(function (err) {
        addStatus.innerHTML = '<span class="error">Error: ' + esc(err) + "</span>";
      })
      .finally(function () {
        running = false;
        addProductsBtn.disabled = false;
        actionBtns.forEach(function (b) { b.disabled = false; });
      });
  }

  // ── Action runner ─────────────────────────────────────────────────────────
  function runAction(endpoint, label) {
    if (running) return;
    running = true;
    actionBtns.forEach(function (b) { b.disabled = true; });
    actionStat.innerHTML = '<span class="loader"></span>Running ' + esc(label) + ' \\u2026';

    api("POST", "api/" + endpoint)
      .then(function (data) {
        if (data.logs && data.logs.length) appendLogs(label, data.logs);

        if (data.skipped) {
          actionStat.innerHTML = '<span class="error">' + esc(label) +
            ": not configured \\u2014 see terminal output.</span>";
        } else if (data.success !== true) {
          actionStat.innerHTML = '<span class="error">' + esc(label) +
            " finished with errors \\u2014 see terminal output.</span>";
        } else {
          var extra = data.updated != null ? " (" + data.updated + " updated)" : "";
          actionStat.innerHTML = '<span class="success">\\u2713 ' + esc(label) +
            " complete" + esc(extra) + ".</span>";
        }
      })
      .catch(function (err) {
        appendLogs(label, [{ level: "ERROR", message: String(err) }]);
        actionStat.innerHTML = '<span class="error">Error running ' +
          esc(label) + ": " + esc(err) + "</span>";
      })
      .finally(function () {
        running = false;
        actionBtns.forEach(function (b) { b.disabled = false; });
      });
  }

  // ── Config badges ─────────────────────────────────────────────────────────
  function loadConfig() {
    api("GET", "api/config")
      .then(function (cfg) {
        if (!cfg.configured) return;
        configCard.style.display = "";

        var disc = cfg.bbuddy_configured
          ? '<span class="badge ok">&#128260; Auto-discover every ' + cfg.discover_interval + ' min</span>'
          : '<span class="badge warn">&#9888;&#65039; Barcode Buddy not configured</span>';
        var gem = cfg.gemini_configured
          ? '<span class="badge ok">&#129302; Gemini AI ready</span>'
          : '<span class="badge warn">&#9888;&#65039; Gemini API key not set</span>';
        configInfo.innerHTML =
          '<span class="badge">Store: ' + esc(cfg.store_id || "\\u2014") + "</span>" +
          disc + gem;
      })
      .catch(function () { /* swallow */ });
  }

  // ── Bind events ───────────────────────────────────────────────────────────
  searchBtn.addEventListener("click", doSearch);
  queryInput.addEventListener("keydown", function (e) { if (e.key === "Enter") doSearch(); });
  selectAllBtn.addEventListener("click", function () { setAllChecked(true); });
  selectNoneBtn.addEventListener("click", function () { setAllChecked(false); });
  addProductsBtn.addEventListener("click", doAddProducts);
  resultsDiv.addEventListener("change", function (e) {
    if (e.target.classList.contains("product-check")) updateSelectionCount();
  });
  discoverBtn.addEventListener("click", function () { runAction("discover", "Discover"); });
  sortBtn.addEventListener("click",     function () { runAction("sort", "Sort"); });
  dateBtn.addEventListener("click",     function () { runAction("date", "Date"); });
  groupBtn.addEventListener("click",    function () { runAction("group", "Group"); });
  updateBtn.addEventListener("click",   function () { runAction("update", "Update"); });
  verboseChk.addEventListener("change", function () {
    verbose = verboseChk.checked;
    terminal.classList.toggle("verbose", verbose);
  });
  clearBtn.addEventListener("click", function () { terminal.innerHTML = ""; });

  // ── Initialise ────────────────────────────────────────────────────────────
  loadConfig();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

# Map of POST endpoint paths to their handler functions.
_POST_HANDLERS: dict[str, Any] = {
    "/api/search": _handle_search,
    "/api/discover": _handle_discover,
    "/api/sort": _handle_sort,
    "/api/date": _handle_date,
    "/api/group": _handle_group,
    "/api/update": _handle_update,
    "/api/add_products": _handle_add_products,
}


class _Handler(BaseHTTPRequestHandler):
    """Route requests to the HTML page or JSON API endpoints."""

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")

        if path in ("/api/config", "api/config"):
            self._json_response(_handle_config())
            return

        # Everything else serves the HTML UI.
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_HTML.encode())

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")

        # Normalise paths: the browser might use relative URLs (no leading /).
        if not path.startswith("/"):
            path = "/" + path

        handler = _POST_HANDLERS.get(path)
        if handler is None:
            self._json_response({"error": "Not found"}, status=404)
            return

        # Read JSON body (if any).
        content_length = int(self.headers.get("Content-Length", 0))
        body: dict[str, Any] = {}
        if content_length > 0:
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                self._json_response({"error": "Invalid JSON"}, status=400)
                return

        # Prevent concurrent long-running operations.
        acquired = _op_lock.acquire(blocking=False)
        if not acquired:
            self._json_response(
                {
                    "success": False,
                    "error": "Another operation is already running. Please wait.",
                },
                status=409,
            )
            return

        try:
            if handler is _handle_search or handler is _handle_add_products:
                result = handler(body)
            else:
                result = handler()
        except Exception as exc:
            logger.exception("API handler %s failed", path)
            result = {"success": False, "error": str(exc)}
        finally:
            _op_lock.release()

        self._json_response(result)

    # Helpers ─────────────────────────────────────────────────────────────────

    def _json_response(self, data: dict[str, Any], status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence per-request access logs."""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    server = HTTPServer(("0.0.0.0", _PORT), _Handler)
    print(f"Ingress server listening on port {_PORT}")
    server.serve_forever()
