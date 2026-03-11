# grocy_scraper

A Python command-line tool that scrapes **[k-ruoka.fi](https://www.k-ruoka.fi/kauppa)** for Finnish food products and their EAN barcodes and populates a **[Grocy](https://grocy.info/)** product database through its REST API.

---

## Features

- Searches or browses the k-ruoka.fi product catalogue using the store's internal JSON API
- Extracts product name and EAN/barcode for each item
- Creates products in Grocy and attaches their EAN barcodes
- Skips products whose barcode is already registered in Grocy (configurable)
- Supports `--dry-run` mode: scrape only, do not write to Grocy
- Paginated fetching with configurable page size and an optional product limit
- Cloudflare Bot Management bypass via FlareSolverr or manual cookie injection
- Configuration via CLI flags *or* environment variables / `.env` file

---

## Requirements

- Python 3.10+
- A running Grocy instance with a valid API key
- The K-group store ID for the store you want to scrape (see [Finding your store ID](#finding-your-store-id))
- A Cloudflare bypass (see [Cloudflare bypass](#cloudflare-bypass)) — required because k-ruoka.fi uses Cloudflare Bot Management

## Installation

```bash
git clone https://github.com/Glitchtit/grocy_scraper.git
cd grocy_scraper
pip install -r requirements.txt
```

---

## Cloudflare bypass

k-ruoka.fi is protected by **Cloudflare Bot Management**.  Without a valid
`cf_clearance` cookie the API returns HTTP 403.  Choose one of these strategies:

### Option A – FlareSolverr (recommended, free)

[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) is a free Docker
service that automatically solves Cloudflare challenges:

```bash
docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```

Then set `FLARESOLVERR_URL` in your `.env` file or environment:

```dotenv
FLARESOLVERR_URL=http://localhost:8191/v1
```

### Option B – Manual cookie injection

1. Open https://www.k-ruoka.fi/kauppa in your browser and pass the challenge.
2. Open DevTools → Application → Cookies → `www.k-ruoka.fi`.
3. Copy the value of the `cf_clearance` cookie and the User-Agent your browser used.
4. Set them in your environment or `.env`:

```dotenv
CF_CLEARANCE=<value>
CF_USER_AGENT=<your browser User-Agent>
```

> **Note:** `cf_clearance` cookies expire after ~1 hour.  Re-run the above steps when you see 403 errors.

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```dotenv
# The store ID from the k-ruoka.fi URL, e.g. https://www.k-ruoka.fi/kauppa?storeId=N110
# Common stores: N110 = K-Supermarket Helsinki, N137 = K-Citymarket Tammisto
KRUOKA_STORE_ID=N110

# Base URL of your Grocy instance
GROCY_BASE_URL=https://grocy.example.com

# API key from Grocy → user settings → Manage API keys
GROCY_API_KEY=your_api_key_here

# Cloudflare bypass (choose one):
FLARESOLVERR_URL=http://localhost:8191/v1
# CF_CLEARANCE=<value>
# CF_USER_AGENT=<your browser User-Agent>
```

All values can also be passed directly as CLI arguments (see [Usage](#usage)).

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

### All options

```
usage: main.py [-h] (--query TERM | --browse)
               [--store STORE_ID] [--max-products N]
               [--grocy-url URL] [--grocy-key KEY]
               [--location-id ID] [--quantity-unit-id ID]
               [--dry-run] [--skip-existing | --no-skip-existing]
               [--verbose]

options:
  --query TERM          Search for products matching this term.
  --browse              Browse the full product catalogue (may be very large).
  --store STORE_ID      K-group store ID (e.g. N110).
                        Also read from KRUOKA_STORE_ID env var.
  --max-products N      Stop after scraping N products.
  --grocy-url URL       Base URL of the Grocy instance.
                        Also read from GROCY_BASE_URL env var.
  --grocy-key KEY       Grocy API key.
                        Also read from GROCY_API_KEY env var.
  --location-id ID      Grocy location ID to assign to new products.
  --quantity-unit-id ID Grocy quantity unit ID to assign to new products.
  --dry-run             Scrape products but do not write to Grocy.
  --skip-existing       Skip products whose EAN is already in Grocy (default).
  --no-skip-existing    Re-add products even if their EAN is already in Grocy.
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
│   ├── scraper.py        # k-ruoka.fi product scraper
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
