# Copilot Instructions

## Build, test, and lint

```bash
pip install -r requirements.txt pytest voluptuous

# Run all tests (414 collected, all expected to pass)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_storage_client.py -v

# Run a single test class or method
python -m pytest tests/test_main.py::TestUpdatePrices::test_update_writes_unit_price_from_krapi -v
```

No linter or formatter is configured.

## Architecture

**Scraper** is a Home Assistant add-on that scrapes Finnish grocery products from **k-ruoka.fi** (with **s-kaupat.fi** and an optional **SearXNG** instance as EAN-lookup fallbacks), then populates **HA-Storage** (a custom SQLite backend) via its REST API — products, barcodes, images, prices (`unit_price`), stores and per-store availability. It contains no AI code: AI optimize is owned by HA-Storage (`/api/ai/optimize`).

### Deployment modes

1. **HA Supervisor Add-on** (`addon/`) — Docker container with s6-overlay running two services: `scraper` (no periodic loop — it just `sleep infinity`s to keep the container alive) and the ingress web server (`ingress_server.py` on port 8099). All work is on demand from the ingress UI / its `/api/*` endpoints (search, discover, update, add_products), which call into `addon/main.py`.
2. **HA Custom Integration** (`custom_components/scraper/`) — Config flow + two actions, `scraper.search_products` and `scraper.add_product` (`services.py` / `services.yaml`). No entities, no sidebar panel (the add-on's ingress panel is the UI; setup removes a stale panel left by versions before 2.1.6).

### Data flow

`ingress_server.py` (HTTP API, tasks) → `main.py` (discover and update pipelines) → `scraper.py` (fetch products) → `storage_client.py` (write to Storage)

### Key modules

| Module | Role |
|---|---|
| `addon/scraper/scraper.py` | K-ruoka.fi scraper (GraphQL + kr-api REST backends) |
| `addon/scraper/storage_client.py` | HA-Storage REST API client |
| `addon/scraper/skaupat_client.py` | S-kaupat.fi EAN lookup (fallback) |
| `addon/scraper/searxng_client.py` | SearXNG EAN lookup (last-resort fallback) |
| `addon/scraper/lookup.py` | `resolve_ean()` — the one lookup cascade over those three; raises `LookupFailed` when K-Ruoka could not be asked |
| `addon/main.py` | The operations the ingress server runs: single and queue discover, update, price budget, store registration |
| `addon/ingress_server.py` | HTTP server for HA ingress web UI; fire-and-poll `/api/*` tasks |
| `custom_components/scraper/services.py` | `scraper.search_products` / `scraper.add_product` handlers |
| `custom_components/scraper/scraperlib/` | Copy of `addon/scraper/` for HACS installs |

### Core data type

`Product` is a `@dataclass` in `scraper.py` with fields `name`, `ean`, `product_id`, `description`, `image_url`, `price`, `comparison_price`, `comparison_unit`, and `extra`. Both scraper backends normalize results into this type (only kr-api fills the price fields). `StoreAvailability` (`store_id`, `available`, `price`, `price_currency`) is the per-store availability result.

### Scraper backends

- **GraphQL** (`KRuokaScraper(store_id)`): `mobile.k-ruoka.fi/graphql`. No Cloudflare bypass. Hard limit of `offset ≤ 1000`. No prices. Used for name/image lookups and per-store availability sweeps.
- **kr-api REST** (`KRuokaScraper(store_id, use_graphql=False)`): `www.k-ruoka.fi/kr-api`. Needs a Cloudflare clearance from FlareSolverr (the `flaresolverr_url` option → `FLARESOLVERR_URL`). The only source of prices and friendly store names; price scrapers are not built without FlareSolverr.

`store_id` may be comma-separated; stores are tried in order. Every EAN lookup goes through `resolve_ean()`: K-Ruoka → S-kaupat → SearXNG (only if `searxng_url` is set). It returns `None` when no source has the product and raises `LookupFailed` when a K-Ruoka store got no answer and S-kaupat did not have it either (SearXNG is then not asked): a single scan fails with a retry message, a queue item stays pending, Update skips the product. A failing fallback while K-Ruoka answered is only logged. `KRuokaScraper.failed_requests` is how a caller tells an empty result from no answer. Update only replaces placeholder names (`Unknown product (…)`, bare barcodes); a real name is never overwritten, whatever the source.

### kr-api rate limiting

kr-api rate-limits aggressively, so (see `scraper.py` and `_update_products` in `main.py`):

- Global jittered pacing across all scrapers: `_KRAPI_MIN_INTERVAL` (2 s, 0.7–1.5× jitter). 429s retry up to 3 times with Retry-After/exponential backoff capped at 120 s.
- Circuit breaker: after 3 consecutive refused requests (429 retries exhausted, or a 403 that a fresh clearance did not fix), all kr-api traffic pauses for `_KRAPI_CIRCUIT_COOLDOWN` (900 s); GraphQL lookups continue.
- Price budget: Update refreshes at most `_PRICE_BUDGET` (200) prices per run, oldest first, skipping ones refreshed within `_PRICE_TTL_SECONDS` (3 days). Only products with a barcode are selected, and a product counts as refreshed only once kr-api answered (`KRuokaScraper.failed_requests` tells "no result" from "no answer"). State in `/data/price_refresh.json`, saved after each product.
- The FlareSolverr clearance is cached in memory and in `/data/cf_clearance.json` for `_CF_CACHE_TTL` (1500 s). A 403 drops it, solves again and retries the request once.
- These are module constants, not options: nothing in the add-on can set an environment variable except `flaresolverr_url` → `FLARESOLVERR_URL`. Tests redirect the two state files to `tmp_path` (`tests/conftest.py`).

### Two copies of the library

The library ships twice and the two copies must stay byte-identical: `addon/scraper/` (the add-on image, and the copy the tests import — `tests/conftest.py` puts `addon/` on `sys.path`) and `custom_components/scraper/scraperlib/` (the HACS integration). Edit `addon/scraper/`, then `cp addon/scraper/*.py custom_components/scraper/scraperlib/`. CI (`.github/workflows/build.yml`) runs the tests and `diff -r -x __pycache__ addon/scraper custom_components/scraper/scraperlib`.

### AI

There is none in this repo — all AI code and options were removed in 1.21.23 (add-on) and 2.1.4 (integration). `/api/add_products` returns `added_ids` so callers (e.g. HA-stock) can POST HA-Storage's `/api/ai/optimize` themselves.

### Retry logic

`_wait_for_storage()` in `ingress_server.py` retries Storage's `/api/health` at start-up (30 attempts × 5 seconds, best-effort), and a background health monitor re-checks every 30 s and re-detects the Storage URL (`storage_url` → `storage_hostname` → auto-detect) if it disappears.

### Error handling

- `StorageAPIError` (defined in `storage_client.py`) is the exception for Storage API failures; `SKaupatError` and `SearXNGError` cover the fallback lookups.
- Log warnings and continue on non-fatal errors; discover/update log and skip per-product failures and keep going.

## Versioning and changelog

Bump all three on user-facing changes:

| File | Field |
|---|---|
| `addon/config.yaml` | `version: "X.Y.Z"` |
| `custom_components/scraper/manifest.json` | `"version": "X.Y.Z"` |
| `addon/CHANGELOG.md` | New `## X.Y.Z` section |

CHANGELOGs use plain `## VERSION` headers (no brackets, no dates).

Follow [Semantic Versioning](https://semver.org/):

| Change type | Bump | Example |
|---|---|---|
| Breaking / incompatible change | **major** | `1.0.0` → `2.0.0` |
| New feature, backward-compatible | **minor** | `1.0.0` → `1.1.0` |
| Bug fix, docs, minor tweak | **patch** | `1.0.0` → `1.0.1` |

Do **not** bump the version or update the changelog for changes that only touch tests, CI configuration, or developer tooling (e.g. `.github/`, `tests/`).

## Conventions

- Options come from `/data/options.json`, read by `ingress_server.py` for every operation (only `flaresolverr_url` is exported to the environment). There is no CLI.
- External API errors are wrapped in `StorageAPIError` / `SKaupatError` / `SearXNGError` rather than leaking `requests` exceptions.
- Tests import `main`, `ingress_server` and the `scraper` package from `addon/`. New tests use the `world` fixture (`tests/conftest.py`): an in-memory `FakeStorage` and a `FakeKRuoka` session (`tests/fakes.py`), so the scraper's own code runs; older tests inject `unittest.mock` objects.
- The scraper yields `Product` objects lazily via generators.
- Product names are in Finnish (Storage canonical names); the ingress UI is in English.
