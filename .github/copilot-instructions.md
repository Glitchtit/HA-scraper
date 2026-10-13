# Copilot Instructions

## Build, test, and lint

```bash
pip install -r requirements.txt pytest

# Run all tests (537 collected; ~150 are pre-existing failures — stale tests for
# AI/Gemini code removed in 1.21.23, in test_main.py and test_ingress_server.py.
# Diff the FAILED set against a clean checkout instead of expecting green.)
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

1. **HA Supervisor Add-on** (`addon/`) — Docker container with s6-overlay running two services: `scraper` (no periodic loop — it just `sleep infinity`s to keep the container alive) and the ingress web server (`ingress_server.py` on port 8099). All work is on demand from the ingress UI / its `/api/*` endpoints (search, discover, update, add_products), which call into `addon/main.py` (also usable as a CLI).
2. **HA Custom Integration** (`custom_components/scraper/`) — Config flow + two actions, `scraper.search_products` and `scraper.add_product` (`services.py` / `services.yaml`). No entities, no sidebar panel (the add-on's ingress panel is the UI; setup removes a stale panel left by versions before 2.1.6).

### Data flow

`main.py` (CLI + orchestration) → `scraper.py` (fetch products) → `storage_client.py` (write to Storage)

### Key modules

| Module | Role |
|---|---|
| `scraper/scraper.py` | K-ruoka.fi scraper (GraphQL + kr-api REST backends) |
| `scraper/storage_client.py` | HA-Storage REST API client |
| `scraper/skaupat_client.py` | S-kaupat.fi EAN lookup (fallback) |
| `scraper/searxng_client.py` | SearXNG EAN lookup (last-resort fallback) |
| `addon/main.py` | Entry point — argparse, `--query`/`--browse`/`--discover`/`--update`/`--delete-all`, discover/update pipelines, price budget |
| `addon/ingress_server.py` | HTTP server for HA ingress web UI; fire-and-poll `/api/*` tasks |
| `custom_components/scraper/services.py` | `scraper.search_products` / `scraper.add_product` handlers |
| `custom_components/scraper/scraperlib/` | Vendored copy of the `scraper/` package for HACS installs |

### Core data type

`Product` is a `@dataclass` in `scraper.py` with fields `name`, `ean`, `product_id`, `description`, `image_url`, `price`, `comparison_price`, `comparison_unit`, and `extra`. Both scraper backends normalize results into this type (only kr-api fills the price fields). `StoreAvailability` (`store_id`, `available`, `price`, `price_currency`) is the per-store availability result.

### Scraper backends

- **GraphQL** (default): `mobile.k-ruoka.fi/graphql`. No Cloudflare bypass. Hard limit of `offset ≤ 1000`. No prices. Used for name/image lookups and per-store availability sweeps.
- **kr-api REST** (fallback): `www.k-ruoka.fi/kr-api`. Requires Cloudflare bypass (FlareSolverr via the `flaresolverr_url` option → `FLARESOLVERR_URL`, or `CF_CLEARANCE` + `CF_USER_AGENT`). The only source of prices and friendly store names; price scrapers are skipped when no bypass is configured.

`store_id` may be comma-separated; stores are tried in order. EAN lookups fall back K-Ruoka → S-kaupat → SearXNG (only if `searxng_url` is set). In `--update`, SearXNG names are low-confidence and only replace placeholder names.

### kr-api rate limiting

kr-api rate-limits aggressively, so (see `scraper.py` and `_update_products` in `main.py`):

- Global jittered pacing across all scrapers: `KRAPI_MIN_INTERVAL` (default 2 s, 0.7–1.5× jitter). 429s retry up to 3 times with Retry-After/exponential backoff capped at 120 s.
- Circuit breaker: after 3 consecutive requests exhaust their retries, all kr-api traffic pauses for `KRAPI_CIRCUIT_COOLDOWN` (default 900 s); GraphQL lookups continue.
- Price budget: `--update` refreshes at most `KRAPI_PRICE_BUDGET` (default 200) prices per run, oldest first, skipping ones refreshed within `KRAPI_PRICE_TTL_DAYS` (default 3). State in `/data/price_refresh.json`.
- The FlareSolverr clearance is cached in `/data/cf_clearance.json` for `CF_CLEARANCE_TTL` (default 1500 s). Both state files honour `SCRAPER_STATE_DIR`.

### Duplicated files

The `scraper/` package is **copied identically** into `addon/scraper/` and `custom_components/scraper/scraperlib/` (hand-synced, no tooling). Changes to `scraper.py`, `storage_client.py`, `skaupat_client.py`, or `searxng_client.py` must be applied to all three locations. Tests import only the top-level `scraper/` copy, so drift will not show up in tests.

### AI

There is none in this repo — all AI code and options were removed in 1.21.23 (add-on) and 2.1.4 (integration). `/api/add_products` returns `added_ids` so callers (e.g. HA-stock) can POST HA-Storage's `/api/ai/optimize` themselves.

### Retry logic

`wait_for_storage()` in `main.py` retries Storage's `/api/health` (30 attempts × 5 seconds), then exits. `_wait_for_storage()` in `ingress_server.py` does the same but is best-effort, and a background health monitor re-checks every 30 s and re-detects the Storage URL (`storage_url` → `storage_hostname` → auto-detect) if it disappears.

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

- Main CLI options have env-var defaults (`KRUOKA_STORE_ID`, `STORAGE_URL`, `GROCY_LOCATION_ID`, `GROCY_QUANTITY_UNIT_ID`, `SEARXNG_URL`); `.env` is loaded via `python-dotenv` when installed. In the add-on, options come from `/data/options.json` (only `flaresolverr_url` is exported to the environment).
- External API errors are wrapped in `StorageAPIError` / `SKaupatError` / `SearXNGError` rather than leaking `requests` exceptions.
- Tests use `unittest.mock` — `StorageClient` and session mocks are injected.
- The scraper yields `Product` objects lazily via generators.
- Product names are in Finnish (Storage canonical names); the ingress UI is in English.
