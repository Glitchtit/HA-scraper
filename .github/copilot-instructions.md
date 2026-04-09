# Copilot Instructions

## Build, test, and lint

```bash
pip install -r requirements.txt pytest

# Run all tests (445 tests)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_storage_client.py -v

# Run a single test class or method
python -m pytest tests/test_main.py::TestAiOptimize::test_optimize_updates_location -v
```

No linter or formatter is configured.

## Architecture

**Scraper** is a Home Assistant add-on that scrapes Finnish grocery products from **k-ruoka.fi** and **s-kaupat.fi**, then populates **HA-Storage** (a custom SQLite backend) via its REST API. It uses Gemini AI for product sorting, dating, grouping, and optimization.

### Deployment modes

1. **HA Supervisor Add-on** (`grocy_scraper_addon/`) — Docker container with s6-overlay running two services: the periodic scraper and an ingress web server (`ingress_server.py` on port 8099). Entry point: `grocy_scraper_addon/main.py`.
2. **HA Custom Integration** (`custom_components/grocy_scraper/`) — Sidebar panel + config flow + WebSocket API.

### Data flow

`main.py` (CLI + orchestration) → `scraper.py` (fetch products) → `storage_client.py` (write to Storage)

### Key modules

| Module | Role |
|---|---|
| `grocy_scraper/scraper.py` | K-ruoka.fi scraper (GraphQL + kr-api REST backends) |
| `grocy_scraper/storage_client.py` | HA-Storage REST API client |
| `grocy_scraper/skaupat_client.py` | S-kaupat.fi EAN lookup |
| `grocy_scraper/searxng_client.py` | SearXNG search client |
| `grocy_scraper/grocy_client.py` | Legacy Grocy client (kept for reference, not used in main flow) |
| `grocy_scraper_addon/main.py` | Entry point — argparse, Gemini AI helpers, `--sort`/`--date`/`--group`/`--optimize` |
| `grocy_scraper_addon/ingress_server.py` | HTTP server for HA ingress web UI |
| `custom_components/grocy_scraper/ws_api.py` | WebSocket API for HA sidebar panel |

### Core data type

`Product` is a `@dataclass` in `scraper.py` with fields `name`, `ean`, and `description`. Both scraper backends normalize results into this type.

### Scraper backends

- **GraphQL** (default): `mobile.k-ruoka.fi/graphql`. No Cloudflare bypass. Hard limit of `offset ≤ 1000`.
- **kr-api REST** (fallback): `www.k-ruoka.fi/kr-api`. Requires Cloudflare bypass.

### Duplicated files

The `grocy_scraper/` package is **copied identically** into `grocy_scraper_addon/grocy_scraper/`. Changes to `scraper.py`, `storage_client.py`, `skaupat_client.py`, or `searxng_client.py` must be applied to both locations.

### Gemini AI integration

All Gemini calls go through `_call_gemini()` → `_call_gemini_json()` in `grocy_scraper_addon/main.py`. Retries up to `_GEMINI_MAX_RETRIES` with exponential back-off. Sanitizes control characters from responses. Batch sizes: 100 for sort/date/group, 1000 for optimize.

### Retry logic

`wait_for_storage()` in `main.py` and `ingress_server.py` retries connecting to Storage on startup (30 attempts × 5 seconds).

### Error handling

- `GrocyAPIError` is the exception for Storage API and Gemini failures (defined in `grocy_client.py`, reused elsewhere).
- Log warnings and continue on non-fatal errors; batch operations skip failed batches.

## Versioning and changelog

Bump all three on user-facing changes:

| File | Field |
|---|---|
| `grocy_scraper_addon/config.yaml` | `version: "X.Y.Z"` |
| `custom_components/grocy_scraper/manifest.json` | `"version": "X.Y.Z"` |
| `grocy_scraper_addon/CHANGELOG.md` | New `## X.Y.Z` section |

CHANGELOGs use plain `## VERSION` headers (no brackets, no dates).

Follow [Semantic Versioning](https://semver.org/):

| Change type | Bump | Example |
|---|---|---|
| Breaking / incompatible change | **major** | `1.0.0` → `2.0.0` |
| New feature, backward-compatible | **minor** | `1.0.0` → `1.1.0` |
| Bug fix, docs, minor tweak | **patch** | `1.0.0` → `1.0.1` |

Do **not** bump the version or update the changelog for changes that only touch tests, CI configuration, or developer tooling (e.g. `.github/`, `tests/`).

## Conventions

- All CLI options have corresponding env vars loaded via `python-dotenv`.
- External API errors are wrapped in `GrocyAPIError` rather than leaking `requests` exceptions.
- Tests use `unittest.mock` — `StorageClient` and session mocks are injected. 445 tests total.
- The scraper yields `Product` objects lazily via generators.
- Product names and UI strings are in Finnish.
