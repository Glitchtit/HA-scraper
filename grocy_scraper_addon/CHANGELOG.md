# Changelog

## 1.21.8
- Log AI token usage after every successful AI call:
  Gemini: prompt/output/total token counts; Ollama: prompt/output tokens + total duration (ms)

## 1.21.7
- Add Ollama support as an alternative AI provider
- New addon config options: `ai_provider` ("gemini"/"ollama"), `ollama_url`, `ollama_model`
- `_call_gemini_json()` routes to `_call_ollama()` when `AI_PROVIDER == "ollama"`
- ingress_server: `_setup_ai_globals()`, `_has_ai()`, `_ai_not_configured_response()` helpers
  so all AI handlers work with either Gemini or Ollama

## 1.21.6
- Fix: `parent_id=""` (empty string) was sent to Storage during optimize/group, causing
  422 errors. Now correctly sends `parent_id=null` to clear the parent relationship.

## 1.21.5
- Multi-pack products (e.g. "Sprite 6-pack") are now merged into their base product
  during optimize: Gemini identifies the base product name, the multi-pack barcode is
  moved (with pack_size set) to the base product, stock is converted (×pack_size), and
  the duplicate multi-pack product is deleted. If no base product exists yet, the
  multi-pack product is renamed to the base name in place.

## 1.21.4
- Fix: during Grocy import, products with no Grocy stock (import_stock_amount=NULL)
  no longer get 1 unit added to stock. Only barcodes that had stock in Grocy get
  their stock amount restored. Manual/scanner barcodes still default to 1 unit.

## 1.21.3
- Fix crash in AI optimize/sort/date/group/dedup steps when Gemini returns a JSON
  array instead of an object; now logs an error and skips the batch instead of crashing

## 1.21.2

- Discover uses import_stock_amount from barcode queue when available (preserves stock during Grocy migration)

## 1.21.1

- Stop deactivating parent products (they are now visible in Storage UI)

## 1.21.0

- StorageClient auto-detects default unit ("kpl") when creating products
- Storage URL auto-detection via container hostname and Supervisor API
- Removed legacy config: location_id, quantity_unit_id, discover_interval
- Replaced storage_url (required) with storage_hostname (optional) in addon config
- Removed periodic discover scheduler from custom component
- Replaced GrocyClient with StorageClient in custom component
- Simplified s6 run script (no more background discover loop)

## 1.20.3

- Add Storage retry logic: wait_for_storage() retries 30× with 5s delay on startup
- Renamed display name from "Grocy Scraper" to "Scraper"

## 1.20.2

- Simplified AI optimization: pack sizes stored on barcode (no separate pack products)
- Single unit_id per product (removed QU field juggling workarounds)
- Leverage CASCADE deletes (no manual barcode/stock cleanup before product deletion)
- Removed orphaned QU consolidation code (Storage seeds canonical units)
- Removed pack weight conversion helper (handled by package size detection)

## 1.20.1

- Remove BarcodebuddyClient; barcode queue in HA-Storage replaces Barcode Buddy
- Deleted bbuddy_url/bbuddy_user/bbuddy_password config options and argparse flags
- Discover now reads pending barcodes from Storage barcode queue and marks items as done
- Added barcode queue methods to StorageClient

## 1.20.0

- Replace GrocyClient with StorageClient for HA-Storage REST API
- Simplified unit handling: single unit_id per product instead of 4 QU fields
- Updated field names: parent_product_id→parent_id, qu_id_stock→unit_id, picture_file_name→picture_filename
- Removed grocy_url/grocy_api_key config options, added storage_url
- Removed duplicate unit consolidation (Storage seeds units once)

## 1.19.10

- Full QU repair for stocked products: delete stock entries, fix all 4 QU fields, then re-add stock
- Parent products inherit quantity unit from children instead of defaulting to kpl
- Add delete_stock_entry method to GrocyClient

## 1.19.9

- Fix products losing default quantity units (stock/purchase/consume/price) when QU consolidation deletes duplicate units
- Fix incremental optimize (single-barcode discover) not repairing orphaned QU fields
- Set all 4 QU fields (stock, purchase, consume, price) when creating new products (was only setting stock + purchase)

## 1.19.8

- Transfer product image from pack to base product during optimize (instead of deleting it)
- If the base product already has an image, the pack's image is deleted as before

## 1.19.7

- Fix pack handling: apply sort/date/group to base product after dissolving multi-packs (was skipped by `continue`)
- Fix pack_of == group_name edge case: un-hide base product when it doubles as the parent
- Fix empty parent deletion incorrectly removing pack base products that have stock and barcodes
- Create per-unit weight QU conversions from pack names (e.g. "580g / 10 kpl" → 58g per piece)

## 1.19.6

- Propagate density conversions from parent products to all child products (Grocy does not inherit product-specific conversions)
- Self-healing density conversions: incremental optimize now runs `_ai_detect_density_conversions()` and checks existing recipes for unit gaps after product discovery
- `_fix_recipe_units()` attempts density creation (via Gemini) before falling back to stock QU for cross-domain gaps
- New `_check_recipes_for_unit_gaps()` scans recipes using a newly discovered product and creates missing weight↔volume conversions

## 1.19.4

- Fix pack handling: add stock to base product when dissolving multi-packs (1 scan = pack_count units)
- Fix incremental unit optimization targeting deleted pack product IDs instead of surviving base products
- Fix wrong conversion factor (e.g. 3l instead of 1.5l) by re-fetching products after pack dissolution

## 1.19.3

- Fix 226 stocked products with deleted QU refs by repairing stock entries first
- Add get_stock_entries() and update_stock_entry() to Grocy API client
- When product QU update fails (stock constraint), fix stock entry qu_id directly, then retry

## 1.19.2

- Fix stocked products with broken units via bridging conversions + per-field fallback
- Create derived cross-domain conversions (kg↔dl, kg↔ml, g↔l, g↔dl, g↔ml) when density conversion is created
- Ensures recipes using dl/ml units can resolve to products stocked in kg/g

## 1.19.1

- Fix broken products with null/empty QU fields (not just deleted unit refs)
- Clean up orphaned product-specific QU conversions referencing deleted units

## 1.19.0

- Fix unit optimization: human-readable log output (unit names instead of numeric IDs)
- Fix 144 failed product QU reassignments by creating bridging conversions before updating stock units
- Add smart default unit detection for broken products (weight→g/kg, volume→l/dl, fallback→kpl)
- Fix conversion migration idempotency (no more "already exists" errors)
- Add recipe unit validation: detect and fix recipe ingredients with missing QU conversions
- Add get_recipe_positions() and update_recipe_position() to Grocy API client

## 1.18.2

- Fix Cloudflare timeout on long-running operations (discover, optimize, sort, date, group, update, search)
- All POST endpoints now return immediately with a task ID; frontend polls for results every 3 seconds
- Add GET `/api/task/{id}` polling endpoint to ingress server
- Fix `api()` JS function to check HTTP status before parsing JSON

## 1.18.1

- Fix single-barcode scan running heavy global optimization: skip parent deduplication and full unit optimization in incremental mode (single product scan)
- Incremental mode now only ensures standard units exist and detects package size for the new product

## 1.18.0

- Add unit optimization to `--optimize`: automatically ensures standard quantity units (g, kg, ml, dl, l, tl, rkl, rs, kpl) and global conversions exist in Grocy
- Consolidate duplicate/synonym quantity units — detects and merges units like "gram", "Gramma", "g" into a single canonical unit
- AI-powered package size detection: extracts sizes from product names (e.g. "Maito 1L" → 1 piece = 1 litre) and creates per-product Piece→unit conversions
- AI-powered density conversions: creates weight↔volume conversions for products (e.g. 1 kg flour ≈ 1.67 L)
- Add quantity unit CRUD methods to Grocy API client

## 1.17.2

- Add connection keep-alive heartbeat to sidebar panel to prevent Cloudflare 524 timeout when page is open for extended periods
- Show reconnect banner with reload button when connection is lost

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
