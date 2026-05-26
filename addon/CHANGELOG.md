## 2.1.6
- The integration no longer registers its own "Scraper" sidebar panel. With both the add-on and this integration installed you got two identical "Scraper" entries in the sidebar — and the integration's bundled `panel.js` was the older, feature-poorer UI (the add-on's ingress panel is the maintained one). The integration keeps its config flow and HA services (`scraper.search_products`, `scraper.add_product`); only the duplicate panel is gone. Setup also removes any stale panel left by an older version, so upgrading clears the duplicate without a full HA restart. Removed the now-dead `ws_api.py` and `www/panel.js` (the WebSocket API existed solely to back that panel — no other consumer).

## 2.1.5
- Multi-store search: `KRuokaScraper` now parses a comma-separated `store_id` and `search()` tries each store in order, returning results from the first store with matches (fixes empty results when configured with multiple stores like `N110,K532,L512,K817`). Change is mirrored across all three `scraper.py` copies (`scraper/`, `addon/scraper/`, `custom_components/scraper/scraperlib/`).

## 2.1.4
- Removed vestigial Gemini AI config fields (`gemini_api_key`, `gemini_model`, `gemini_model_optimize`) from integration options and all associated constants, schema, and translation strings. These were orphaned when AI optimize moved to HA-Storage in v1.21.23.
- Removed the orphaned Sort and Date panel actions (`scraper/run_sort`, `scraper/run_date` WebSocket commands and their sync helpers) that called `_ai_sort_products` / `_ai_assign_due_dates` — functions deleted from the add-on in v1.21.23. Calling either button would have raised `AttributeError` at runtime.
- Scraper integration options are now just Upload images and Use GraphQL. No effect on `search_products`, `add_product`, or discover.

## 2.1.3
- Fix options flow ("Configure" button) crashing on modern Home Assistant: `OptionsFlow.config_entry` is now a read-only property, so the old `self.config_entry = config_entry` assignment in `__init__` raised `AttributeError: property 'config_entry' ... has no setter`. Removed the custom `__init__` and the `config_entry` argument; the flow now uses HA's auto-provided `self.config_entry`.

## 2.1.2
- Vendor the scraper backend package into `custom_components/scraper/scraperlib/` so the integration's `search_products`/`add_product` services and the WS panel work on a clean HACS install without requiring a manual `/config/scraper/` copy. Previously the integration called `_ensure_repo_on_path()` to find the repo-root `scraper/` package, which only worked in the add-on Supervisor environment and broke on standalone HACS installs with `ModuleNotFoundError: No module named 'scraper'`.
- All `from scraper.X import Y` calls in `services.py` and `ws_api.py` are now `from .scraperlib.X import Y` (relative imports into the vendored subpackage). `_ensure_repo_on_path()` is retained in `ws_api.py` only for the three `from addon import main` call-sites that still require the repo root on sys.path (run_discover / run_sort / run_date workflows); it has been removed from `services.py` entirely.
- Note: `scraperlib/` is now a third in-repo copy of the shared package (alongside `scraper/` and `addon/scraper/`). When `scraper.py`, `storage_client.py`, `skaupat_client.py`, or `searxng_client.py` change, keep all three copies in sync.

## 2.1.1
- Fix integration failing to load on Home Assistant 2025.x: replace the removed `hass.http.register_static_path` with `async_register_static_paths` + `StaticPathConfig` in `async_setup`. This crash prevented the whole `scraper` component (and thus the `scraper.search_products` / `scraper.add_product` services) from setting up.

## 2.1.0
- Added two agent-callable Home Assistant services to the integration: `scraper.search_products` (response-only) wraps the same K-Ruoka product search as the panel's `scraper/search` WebSocket command and returns `{products: [{name, ean, description, image_url}]}`; `scraper.add_product` creates a found product in HA-Storage (product → optional barcode → optional image download+upload) and returns `{product_id}`
- The two services let an LLM/agent discover an unknown grocery product and add it to Storage so it can be put on the shopping list, without opening the sidebar panel
- Image download for `scraper.add_product` uses `requests` inside an executor job to match the integration's existing synchronous `StorageClient`; no new manifest dependency was added

## 2.0.1
- Cleanup: removed dead Barcode Buddy block (`BARCODEBDY_*` env vars) from `.env.example`. Barcode discovery has been routed through HA-Storage's barcode queue for many releases; the placeholder vars served no purpose

## 2.0.0
- **BREAKING**: removed all "grocy" naming. The add-on is now `scraper` (was `grocy_scraper`); the HACS integration's `domain` is now `scraper` (was `grocy_scraper`). Existing installations must be **uninstalled and reinstalled** — Home Assistant treats the renamed add-on as a new add-on, and integration entities under `grocy_scraper.*` are orphaned. Recreate any automations referencing `grocy_scraper.*` entities under `scraper.*`
- Repo renamed from `grocy_scraper` to `HA-scraper` on GitHub. The old URL still 301-redirects, but bookmarks/integrations should be updated
- Internal: Python package `grocy_scraper/` → `scraper/`, addon dir `grocy_scraper_addon/` → `addon/`, HACS integration dir `custom_components/grocy_scraper/` → `custom_components/scraper/`, s6 service `grocy_scraper` → `scraper`
- WebSocket action namespaces: `grocy_scraper/search`, `grocy_scraper/run_discover`, `grocy_scraper/get_config`, `grocy_scraper/run_sort`, `grocy_scraper/run_date` → `scraper/*`. Custom element `grocy-scraper-panel` → `scraper-panel`
- Misleading vestigial naming cleaned up: local variables `grocy = StorageClient(...)` → `storage`, `grocy_id` → `product_id`, `GrocyAPIError` mentions in docs → `StorageAPIError`, env var `GROCY_BASE_URL` → `STORAGE_BASE_URL`
- The add-on writes to HA-Storage (has done so for many releases); the old name was carried for historical reasons

## 1.21.23
- Removed all remaining AI code from the scraper now that HA-Storage owns AI optimize. Deleted unreachable unit-optimisation chain (`_optimize_units`, `_ai_detect_package_sizes`, `_ai_detect_density_conversions`, `_fix_recipe_units`, `_fix_broken_product_units`, `_merge_recipe_stubs`, `_check_recipes_for_unit_gaps`, `_derive_density_conversions`, `_ensure_units_and_conversions`, `_canonical_unit`) and the provider abstraction (`_call_gemini*`, `_call_ollama`, `_call_claude`, `_extract_json_text`)
- Removed `ai_provider`, `gemini_api_key`, `gemini_model`, `ollama_url`, `ollama_model`, `claude_api_key`, `claude_model` from add-on options/schema
- Removed `--gemini-api-key` and `--gemini-model` CLI flags
- Removed `_setup_ai_globals`, `_has_ai`, `_ai_not_configured_response` from ingress server; `/api/config` no longer reports AI status
- Dropped `anthropic` from requirements

## 1.21.22
- Cleanup: removed unused `gemini_model_optimize` option from add-on config (was only used by the deleted `_ai_optimize_products`)
- Cleanup: deleted ~1700 lines of dead AI categorisation code from `main.py`

## 1.21.21
- AI product optimisation is now fully owned by HA-Storage; the scraper no longer chains `_ai_optimize_products` after `/api/discover` (single + batch) or `/api/add_products`. Callers (e.g. HA-grocy-stock) should POST to HA-Storage's `/api/ai/optimize` directly with the returned product ids
- `/api/add_products` now returns `added_ids` so callers can target the AI optimize at exactly the products that were just created

## 1.21.20
- Fix: strip spurious leading zeros from EAN codes returned by k-ruoka.fi (e.g. `0000090493508` → `90493508`) so downstream EAN lookups in HA-grocy-stock no longer fail on EAN-8 codes left-padded to 13 digits

## 1.21.19
- `ai_provider` schema changed to dropdown (list) — renders as radio buttons in HA add-on options instead of free text

## 1.21.18
- Cleanup: removed legacy Grocy/Barcode Buddy compatibility shims from StorageClient; all field names now use Storage-native names (abbreviation, from_unit_id, to_unit_id, unit_id)
- Cleanup: deleted dead grocy_client.py module (GrocyClient not used anywhere)
- Cleanup: updated HA integration strings/translations to reference HA-Storage instead of Grocy
- Fix: create_quantity_unit argument order corrected (was passing name_plural as abbreviation)

## 1.21.17
- Optimizer: ALL drinks of any kind always assigned to Fridge/refrigerator (explicit rule, no exceptions)

## 1.21.16
- Fix: SearXNG Strategy 2 now rejects untrusted domains (trademark/patent/legal databases) as product name sources; only known grocery and product sites are trusted — previously EAN numbers appearing in trademark documents caused correct images but completely wrong product names

## 1.21.15
- Remove Optimize / Sort / Date / Group bulk AI buttons from ingress UI (moved to Storage app)
- Full AI optimize now runs in Storage app with live log streaming and progress UI

## 1.21.14
- Persistent Storage health monitoring: background thread runs forever, re-detects Storage if it goes down or moves; startup no longer fatal if Storage is unavailable

# Changelog

## 1.21.13
- Fix Claude JSON parsing: extract JSON from markdown fences or prose-prefixed responses
- Add `_extract_json_text()` helper used for claude and ollama providers in `_call_gemini_json()`

## 1.21.12
- Add Claude AI provider support (anthropic SDK)
- `claude_api_key` and `claude_model` options in addon config
- `_call_claude()` function in main.py routed via `_call_gemini_json()` dispatcher
- Startup log now reports Claude provider + model
- `_has_ai()` and `_setup_ai_globals()` updated for claude provider

## 1.21.11
- Optimize: 3-phase batched pipeline replaces single 1000-item AI call
  - Phase 1: establish categories + parent products in 100-item batches with
    progressive chaining (each batch reuses names from prior batches)
  - Phase 2: assign locations, best-before dates, pack info in 100-item batches
  - Incremental mode: combined structure+details call in 100-item batches
  - Eliminates timeout failures on large catalogues

## 1.21.10
- Fix optimize: pack_size no longer incorrectly set for products like cotton swabs
  (200kpl = 200 items in one package, not a 200-pack of identical units)
- Fix optimize: hygiene/personal care products now correctly assigned to Bathroom location
- Fix optimize: product's current location is shown to AI to prevent overriding correct assignments

## 1.21.9
- Storage version now read dynamically from config.json (fixes "0.1.0" shown on every startup)
- Log AI provider + model on ingress_server startup (Gemini key/model or Ollama url/model)
- All "Asking Gemini…" and "Gemini batch…" log messages changed to "AI" (provider-agnostic)

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
