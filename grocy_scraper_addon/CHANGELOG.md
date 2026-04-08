# Changelog

## 1.17.1

- Fix ingress server blocking: switch to threaded HTTP server so long-running operations (discover/optimize) don't block health-check probes and UI requests
- Suppress noisy BrokenPipeError tracebacks from disconnected clients

## 1.17.0

- Configurable AI model for optimize: add `gemini_model_optimize` option to use a stronger model for full optimize/group operations while keeping the lightweight model for discover and single-product tasks
- Improved product category grouping: AI now creates practical, kitchen-shelf categories instead of overly broad industrial taxonomy (e.g. dairy splits into Maito, Voi, Juusto instead of one giant Maitotaloustuotteet)
- When `gemini_model_optimize` is empty, falls back to the regular `gemini_model` (backward-compatible)

## 1.16.1

- Protect user "keep in stock" choices: optimize and group skip parent assignment for products with min_stock_amount > 0, still applying product group (category)
- Products deliberately detached from parents to enable min_stock are preserved across optimize runs

## 1.16.0

- Clean-slate optimize: `--optimize` now strips ALL existing parent assignments and rebuilds the entire parent/group structure from scratch using AI
- Full mode sends every leaf product to Gemini without existing parent/category hints, allowing the AI to determine the ideal structure
- Old parent-only placeholder products are automatically deleted when they receive no new children
- Unused product groups are cleaned up after rebuild
- Discover mode (`--discover`) remains incremental — new products are slotted into the existing structure with dedup and parent/category hints
- Fix in-memory parent clearing bug that prevented stripped-child products from being reused as parents

## 1.15.0

- Separate parent products from product groups: parent products are now many and detailed (e.g. "Mustapippuri", "Oregano"), while product groups are few and broad categories (e.g. "Mausteet" for all spices, "Makeiset" for all candy)
- Add `category` field to both group and optimize Gemini prompts for broad product group assignment
- Existing product category names are now included as prompt hints to encourage reuse
- Revert v1.14.3 product group sync in dedup (was syncing to narrow parent names)

## 1.14.3

- Fix: dedup now syncs product groups with parent products — when merging synonymous parents, children's `product_group_id` is updated to match the canonical group, and orphaned product groups are deleted

## 1.14.2

- Fix: prevent optimize/group from undoing dedup merges — when Gemini suggests a group name that was just merged away by dedup (e.g. "Karkki" after it was merged into "Makeiset"), the name is now redirected to the canonical parent instead of recreating a duplicate

## 1.14.1

- Fix: add parent product deduplication before optimize and group — synonymous parents (e.g. "Mauste", "Mausteet", "Mausteseos") are now merged into one canonical parent using Gemini before the main optimize runs, preventing products from bouncing between similar categories

## 1.14.0

- Fix: optimize now includes existing parent product names in the Gemini prompt so that per-product optimize runs (e.g. after barcode scanning) reuse existing groups instead of creating duplicates like "Mauste", "Mausteet", "Mausteseos"
- Fix: optimize can now re-group products that were previously assigned to a wrong parent — running `--optimize` on the full database corrects inconsistent groupings
- Fix: empty parent products (zero children remaining) are automatically cleaned up after optimize
- Fix: the `updated` counter now includes date and group changes (previously only counted location + pack updates)
- The standalone `--group` command also includes existing parent names in its Gemini prompt for consistency

## 1.13.3

- Improve Barcode Buddy cleanup: remove ALL matching entries for a barcode (not just the first), and log failures at WARNING level instead of DEBUG

## 1.13.2

- Fix: single-barcode discover (from HA-grocy-stock scan) now removes the barcode from Barcode Buddy's unknown/pending list after a successful find

## 1.13.1

- Fix: periodic discover (s6 loop) now exports `GEMINI_API` and `GEMINI_MODEL` env vars so the AI optimize step runs automatically on newly discovered products

## 1.13.0

- SearXNG fallback: when a product EAN is not found via K-Ruoka or S-kaupat, the add-on can now search a self-hosted SearXNG instance to discover product info from web results (especially k-ruoka.fi product pages). Configure via the new `searxng_url` add-on option.
- Kesko image CDN integration: product images are fetched from the Kesko CDN without store context for SearXNG-discovered products.

## 1.12.0

- Remove deprecated standalone CLI (`main.py`); `grocy_scraper_addon/main.py` is now the single entry point
- Redirect all test and integration imports to `grocy_scraper_addon.main`

## 1.11.1

- Fix: sync `_discover_single_barcode()` to add-on `main.py`, fixing `AttributeError` when scanning barcodes via Grocy Stock

## 1.11.0
- Increase Gemini API timeout from 60s to 300s to prevent read timeouts on large optimize batches

## 1.10.0
- New `--optimize` CLI flag and `/api/optimize` ingress endpoint that consolidates AI sort, date, group, and **pack detection** into a single Gemini prompt per batch. Multi-packs (e.g. "Red Bull 4-pack") are detected and their barcodes moved to the base product with the correct amount; the pack product is then deleted.
- `GrocyClient.update_barcode()` and `get_product_barcodes()` methods for barcode manipulation.
- ✨ Optimize button in the ingress web UI.
- Optimize batch size increased to 1000 products (up from 100 for individual sort/date/group) to leverage modern LLM context windows and give the AI a better overview of the whole database.
- Discover and add-products pipelines now chain `_ai_optimize_products()` instead of three separate sort/date/group calls.

## 1.9.0
- Ingress search: after adding products via `/api/add_products`, automatically runs AI sort, date, and group on the newly added products when a Gemini API key is configured (same behaviour as the discover pipeline).

## 1.8.3
- Discover: AI sort, date, and group operations now apply only to newly discovered products instead of the entire Grocy catalogue. Standalone `--sort`, `--date`, and `--group` commands continue to operate on all products.

## 1.8.2
- Gemini AI sort/date/group: retry up to 3 times with exponential back-off when the API returns invalid JSON (e.g. control characters, HTML error pages). Responses are sanitized by stripping stray control characters before parsing.

## 1.8.0
- Sort: after assigning new default locations, existing stock is now automatically transferred (moved) to the sorted location via the Grocy stock transfer API.

## 1.7.0
- Discover now automatically runs AI Sort, Date, and Group on discovered products when a Gemini API key is configured. Applies to both CLI (`--discover --gemini-api-key KEY`) and Add-on ingress (`/api/discover`).

## 1.6.0
- CLI / Add-on: support multiple K-group store IDs (comma-separated) via `--store` / `KRUOKA_STORE_ID`. If a scrape fails for the first store, the next store is tried automatically. Applies to search, browse, discover, and update modes.

## 1.5.5
- Add-on: set `panel_admin: false` in `config.yaml` so the sidebar entry is visible to non-admin users (previously defaulted to `true`, hiding the panel from non-admins).

## 1.5.4
- Ingress UI: product images are now uploaded to Grocy when adding products via the search → add flow (`/api/add_products`). Respects the `upload_images` add-on option (default: true).

## 1.5.3
- Group: prevent "Unsupported product nesting level" errors by skipping products that already have sub-products (are parents) and by not reusing existing products that are already children as parent products.

## 1.5.2
- Group: master (parent) products are now assigned to the "Group master" product group and marked with "Never show on stock overview" (`hide_on_stock_overview`), keeping the stock overview clean.
- Group: a product group matching the parent name (e.g. "Maito") is created for each group and assigned to every child product.
- Group: removed category exclusions — snacks, candy, soft drinks, energy drinks, and alcoholic beverages are now grouped like all other products (e.g. energy drinks → "Energiajuoma", potato chips → "Sipsi").

## 1.5.1
- Add-on: sync `main.py` and `scraper.py` copies in `grocy_scraper_addon/` with root, fixing `AttributeError: module 'main' has no attribute '_ai_group_products'` when using the `/api/group` endpoint.

## 1.5.0
- CLI: new `--group` flag that uses Gemini AI to analyse the product database and group similar products (e.g. different brands of milk) under a shared parent product.  Parent products are created automatically with "Accumulate sub products min. stock amount" enabled.
- Add-on: new Group action button and `/api/group` REST endpoint in the ingress UI.

## 1.4.3
- Search: multi-word queries now send only the first word to the upstream API and filter client-side, fixing fuzzy search for non-contiguous words (e.g. "lotus paperi" now correctly finds "Lotus Soft Embo 8 rll wc-paperi").

## 1.4.2
- Search: use fuzzy multi-word matching so that every query word is matched independently (e.g. "lotus paperi" now finds "Lotus Soft Embo 8 rll wc-paperi").

## 1.4.1
- Sort: log messages now display the location name instead of the numeric location ID.

## 1.4.0
- UI: live-streaming terminal output for Discover, Sort, and Date actions — log lines now appear in real time instead of all at once after the operation completes.
- UI: default search max results reduced from 50 to 10.

## 1.3.0
- UI: product selection checkboxes on search result cards with "Select All", "Select None", and "Add Products" buttons above the results grid.
- Add-on: new `/api/add_products` REST endpoint to add selected products to the Grocy database (creates product, associates barcode, skips duplicates).

## 1.2.1
- UI: dark theme applied to both the add-on ingress page and the Home Assistant sidebar panel (updated all hardcoded and fallback colours to dark palette).

## 1.2.0
- Add-on: interactive ingress UI accessible via the sidebar button. Users can now search products, run Discover, Sort, Date, and Update operations with action buttons, and view console/log output in a terminal pane with a verbose toggle.
- Add-on: REST API endpoints (`/api/search`, `/api/discover`, `/api/sort`, `/api/date`, `/api/update`, `/api/config`) served by the ingress web server.

## 1.1.3
- Add-on: export `GROCY_LOCATION_ID` and `GROCY_QUANTITY_UNIT_ID` from add-on config so that `main.py` receives the values set in the UI.
- CLI: environment-variable defaults for `--location-id` and `--quantity-unit-id` are now properly converted to `int` (argparse `type=int` does not apply to defaults).

## 1.1.2
- Add-on: resolved `s6-overlay-suexec: fatal: can only run as pid 1` error by adding `init: false` to `config.yaml` (prevents Docker `--init` from displacing s6-overlay as PID 1) and migrating service definitions from the deprecated `/etc/services.d/` (s6-overlay v2) to `/etc/s6-overlay/s6-rc.d/` (s6-overlay v3).

## 1.1.1
- Add-on: removed legacy `CMD ["/run.sh"]` in favour of proper s6-overlay service directories.
- Add-on: enabled ingress with `ingress: true`, `ingress_port`, `panel_icon`, and `panel_title` in `config.yaml` so the "Show in sidebar" toggle is available.
- Add-on: added a minimal ingress web server (`ingress_server.py`) to serve a status page when the sidebar entry is opened.

## 1.1.0
- Add-on config (`config.yaml`): added missing `location_id`, `quantity_unit_id`, `flaresolverr_url`, `gemini_api_key`, and `gemini_model` options and schema entries.
- `.env.example`: added `GROCY_LOCATION_ID`, `GROCY_QUANTITY_UNIT_ID`, `GEMINI_API`, and `GEMINI_MODEL` entries.

## 1.0.0
- Initial release.
- K-Ruoka GraphQL scraper with category browsing.
- K-Ruoka REST API fallback with Cloudflare bypass support.
- Grocy REST API client for product creation and management.
- Home Assistant add-on packaging.
- Home Assistant custom integration with frontend panel.
- Barcode Buddy integration for automated product discovery.
