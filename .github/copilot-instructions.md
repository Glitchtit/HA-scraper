# Copilot Instructions

## Build, test, and lint

```bash
pip install -r requirements.txt pytest

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_scraper.py -v

# Run a single test class or method
python -m pytest tests/test_grocy_client.py::TestCreateProduct -v
python -m pytest tests/test_main.py::TestSyncProduct::test_creates_new_product -v
```

There is no linter or formatter configured.

## Architecture

This is a CLI tool that scrapes Finnish grocery products from **k-ruoka.fi** and populates a **Grocy** self-hosted product database via its REST API.

### Data flow

`main.py` (CLI + orchestration) → `grocy_scraper/scraper.py` (fetch products) → `grocy_scraper/grocy_client.py` (write to Grocy)

- **`main.py`** — Entry point. Parses CLI args (which also fall back to env vars / `.env`), validates config, then iterates over scraped `Product` objects and syncs each to Grocy via `sync_product()`.
- **`scraper.py`** — `KRuokaScraper` with two backends selected by `use_graphql` flag:
  - **GraphQL** (default): `mobile.k-ruoka.fi/graphql`. No Cloudflare bypass. Hard limit of `offset ≤ 1000` per query (~1,100 results). `browse()` works around this by iterating 27 hardcoded `_PRODUCT_CATEGORY_SLUGS`.
  - **kr-api REST** (fallback): `www.k-ruoka.fi/kr-api`. Requires Cloudflare bypass (FlareSolverr, manual cookies, or curl_cffi TLS impersonation).
- **`grocy_client.py`** — Thin `requests`-based client. Auth via `GROCY-API-KEY` header. Wraps all API errors in `GrocyAPIError`.

### Core data type

`Product` is a `@dataclass` in `scraper.py` with fields `name`, `ean`, and `description`. Both backends normalize results into this type.

## Conventions

- All CLI options have corresponding env vars (e.g. `--store` / `KRUOKA_STORE_ID`). Env vars are loaded via `python-dotenv` from `.env`.
- External API errors are wrapped in domain-specific exceptions (`GrocyAPIError`) rather than leaking `requests` exceptions.
- Tests use `unittest.mock` (no external mocking libraries). The `GrocyClient` accepts an optional `session` parameter for injecting a mock `requests.Session`. The scraper tests inject mock sessions directly onto scraper instances.
- GraphQL responses can contain two union types (`Product` and `AssortmentSearchResult`) — both must be handled when parsing results.
- The scraper yields `Product` objects lazily via iterators (generators), so callers process results in a streaming fashion.
