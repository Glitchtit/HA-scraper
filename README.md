# grocy_scraper

A Python command-line tool that scrapes **[k-ruoka.fi](https://www.k-ruoka.fi/kauppa)** for Finnish food products and their EAN barcodes and populates a **[Grocy](https://grocy.info/)** product database through its REST API.

---

## Features

- Two backends – pick the one that works for you:
  - **GraphQL** (default) – `mobile.k-ruoka.fi/graphql`, no Cloudflare bypass needed
  - **kr-api REST** (fallback) – `www.k-ruoka.fi/kr-api`, requires a CF bypass
- Searches or browses the k-ruoka.fi product catalogue by keyword or full catalogue
- Handles both `Product` and `AssortmentSearchResult` GraphQL types
- Full category-based catalogue browsing via 27 hardcoded K-group category slugs
- Extracts product name and EAN/barcode for each item
- Creates products in Grocy and attaches their EAN barcodes
- Skips products whose barcode is already registered in Grocy (configurable)
- Supports `--dry-run` mode: scrape only, do not write to Grocy
- Configuration via CLI flags *or* environment variables / `.env` file

---

## Requirements

- Python 3.10+
- A running Grocy instance with a valid API key
- The K-group store ID for the store you want to scrape (see [Finding your store ID](#finding-your-store-id))
- **No Cloudflare bypass needed** when using the default GraphQL backend

## Installation

```bash
git clone https://github.com/Glitchtit/grocy_scraper.git
cd grocy_scraper
pip install -r requirements.txt
```

---

## Backends

### GraphQL (default, recommended)

The mobile app GraphQL API at `https://mobile.k-ruoka.fi/graphql` — **no Cloudflare bypass required**.  Simply install and run:

```bash
python main.py --store N110 --browse --dry-run
```

**Limitations**: The server enforces an `offset ≤ 1000` hard limit, so a single
query returns at most 1,100 products.  The `browse()` mode works around this by
iterating all 27 category slugs, fetching up to 1,100 products per category.
Very large categories (> 1,100 products) will be partially fetched.

### kr-api REST (fallback)

The web SPA REST API at `https://www.k-ruoka.fi/kr-api` — requires a Cloudflare
bypass.  Use `--no-graphql` to activate it:

```bash
python main.py --store N110 --browse --no-graphql --dry-run
```

**Cloudflare bypass** — choose one strategy:

#### Option A – FlareSolverr (recommended, free)

```bash
docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```

Set in `.env`:
```dotenv
FLARESOLVERR_URL=http://localhost:8191/v1
```

#### Option B – Manual cookie injection

1. Open https://www.k-ruoka.fi/kauppa in your browser and pass the challenge.
2. Open DevTools → Application → Cookies → `www.k-ruoka.fi`.
3. Copy the value of `cf_clearance` and your browser's User-Agent.

```dotenv
CF_CLEARANCE=<value>
CF_USER_AGENT=<your browser User-Agent>
```

> **Note:** `cf_clearance` cookies expire after ~1 hour.

#### Option C – curl_cffi TLS impersonation

Install the optional dependency and it is tried automatically if no cookies are provided:

```bash
pip install curl_cffi
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```dotenv
KRUOKA_STORE_ID=N110
GROCY_BASE_URL=https://grocy.example.com
GROCY_API_KEY=your_api_key_here
GEMINI_API=your_gemini_api_key_here
GEMINI_MODEL=gemini-1.5-flash
```

---

## Usage

### Search for specific products

```bash
python main.py --store N110 --query "maito" \
    --grocy-url https://grocy.example.com --grocy-key MY_API_KEY
```

### Browse the full catalogue

```bash
python main.py --store N110 --browse \
    --grocy-url https://grocy.example.com --grocy-key MY_API_KEY
```

### Limit the number of products

```bash
python main.py --store N110 --browse --max-products 100 \
    --grocy-url https://grocy.example.com --grocy-key MY_API_KEY
```

### Dry-run (scrape only, do not write to Grocy)

```bash
python main.py --store N110 --query "juusto" --dry-run
```

### AI: assign products to locations (--sort)

Uses Gemini AI to read your Grocy product list and available locations, then
sets each product's storage location to the most appropriate one (e.g. dairy →
fridge, cleaning supplies → cleaning cabinet).  Requires a Gemini API key.

```bash
python main.py --sort \
    --grocy-url https://grocy.example.com --grocy-key MY_API_KEY \
    --gemini-api-key MY_GEMINI_KEY
```

### AI: set default best-before days (--date)

Uses Gemini AI to estimate typical best-before days for every product in your
Grocy database and updates the `default_best_before_days` field accordingly.

```bash
python main.py --date \
    --grocy-url https://grocy.example.com --grocy-key MY_API_KEY \
    --gemini-api-key MY_GEMINI_KEY
```

You can combine `--sort` and `--date` in a single run, and also combine them
with `--browse` or `--query` to scrape *and* analyse in one step:

```bash
python main.py --sort --date --browse --store N110 \
    --grocy-url https://grocy.example.com --grocy-key MY_API_KEY \
    --gemini-api-key MY_GEMINI_KEY \
    --location-id 2 --quantity-unit-id 2
```

### Use the kr-api fallback backend

```bash
python main.py --store N110 --browse --no-graphql \
    --grocy-url https://grocy.example.com --grocy-key MY_API_KEY
```

### All options

```
usage: main.py [-h] (--query TERM | --browse | --sort | --date)
               [--store STORE_ID] [--max-products N]
               [--grocy-url URL] [--grocy-key KEY]
               [--location-id ID] [--quantity-unit-id ID]
               [--gemini-api-key KEY]
               [--dry-run] [--skip-existing | --no-skip-existing]
               [--upload-images] [--no-graphql] [--verbose]

options:
  --query TERM          Search for products matching this term.
  --browse              Browse the full product catalogue.
  --store STORE_ID      K-group store ID (e.g. N110, N137).
                        Also read from KRUOKA_STORE_ID env var.
  --max-products N      Stop after scraping N products.
  --grocy-url URL       Base URL of the Grocy instance.
                        Also read from GROCY_BASE_URL env var.
  --grocy-key KEY       Grocy API key.
                        Also read from GROCY_API_KEY env var.
  --location-id ID      Grocy location ID to assign to new products.
  --quantity-unit-id ID Grocy quantity unit ID to assign to new products.
  --sort                Use Gemini AI to assign each product in the Grocy
                        database to the most appropriate available location.
  --date                Use Gemini AI to set default best-before days for
                        each product in the Grocy database.
  --gemini-api-key KEY  Gemini API key for --sort / --date analysis.
                        Also read from GEMINI_API env var.
  --gemini-model MODEL  Gemini model to use for --sort / --date analysis
                        (default: gemini-1.5-flash).
                        Also read from GEMINI_MODEL env var.
  --dry-run             Scrape products but do not write to Grocy.
  --skip-existing       Skip products whose EAN is already in Grocy (default).
  --no-skip-existing    Re-add products even if their EAN is already in Grocy.
  --upload-images       Download and upload product images to Grocy.
  --no-graphql          Use the kr-api REST backend instead of GraphQL.
                        Requires a Cloudflare bypass (see .env.example).
  -v, --verbose         Enable DEBUG logging.
```

---

## Finding your store ID

1. Go to [k-ruoka.fi/kauppa](https://www.k-ruoka.fi/kauppa).
2. Select your local store.
3. The store ID appears in the URL: `https://www.k-ruoka.fi/kauppa?storeId=`**`N110`**.

Common store IDs: `N110` (K-Supermarket Helsinki), `N137` (K-Citymarket Tammisto).

---

## Project structure

```
grocy_scraper/
├── grocy_scraper/
│   ├── __init__.py
│   ├── scraper.py        # k-ruoka.fi product scraper (GraphQL + kr-api backends)
│   └── grocy_client.py   # Grocy REST API client
├── tests/
│   ├── test_scraper.py
│   ├── test_grocy_client.py
│   └── test_main.py
├── main.py               # CLI entry point
├── requirements.txt
└── .env.example
```

---

## Running tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

---

## Grocy API reference

The tool uses the following Grocy endpoints:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET`  | `/api/stock/products/by-barcode/{barcode}` | Check if a barcode already exists |
| `POST` | `/api/objects/products` | Create a new product |
| `POST` | `/api/objects/product_barcodes` | Add an EAN barcode to a product |
| `GET`  | `/api/objects/products` | List all products |
| `GET`  | `/api/objects/product_barcodes` | List all registered barcodes |

Authentication is performed via the `GROCY-API-KEY` HTTP header.

---

## Legal notice

Scraping k-ruoka.fi is subject to their [terms of service](https://www.k-ruoka.fi).  
This tool is intended for personal use only.  The author is not affiliated with K-ryhmä or K-ruoka.fi.
