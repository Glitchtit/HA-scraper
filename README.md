# grocy_scraper

A **[Home Assistant](https://www.home-assistant.io/) custom integration** and **Home Assistant Supervisor add-on** that scrapes **[k-ruoka.fi](https://www.k-ruoka.fi/kauppa)** for Finnish food products and their EAN barcodes and populates a **[Grocy](https://grocy.info/)** product database through its REST API.

---

## Home Assistant Supervisor add-on

The `grocy_scraper_addon/` directory contains a full Home Assistant Supervisor add-on that runs the scraper in a dedicated Docker container managed by HA Supervisor.

### Add-on installation

1. In Home Assistant, navigate to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** (three-dot) menu in the top-right and choose **Repositories**.
3. Add `https://github.com/Glitchtit/grocy_scraper` and click **Add**.
4. The **Grocy Scraper** add-on will appear in the store — click **Install**.
5. Open the add-on's **Configuration** tab and fill in at minimum:
   - `grocy_url` — base URL of your Grocy instance
   - `grocy_api_key` — Grocy API key
   - `store_id` — K-group store ID (see [Finding your store ID](#finding-your-store-id))
6. Click **Save** and then **Start**.

### Add-on options

| Option | Description | Default |
|--------|-------------|---------|
| `grocy_url` | Base URL of your Grocy instance (no trailing slash) | *(required)* |
| `grocy_api_key` | Grocy API key | *(required)* |
| `store_id` | K-group store ID (e.g. `N110`) | `N110` |
| `discover_interval` | Minutes between automatic `--discover` runs | `60` |
| `upload_images` | Download and upload product images | `true` |
| `use_graphql` | Use the faster GraphQL backend (recommended) | `true` |
| `bbuddy_url` | Base URL of your Barcode Buddy instance | *(optional)* |
| `bbuddy_api_key` | Barcode Buddy API key | *(optional)* |
| `bbuddy_user` | Barcode Buddy web UI username | *(optional)* |
| `bbuddy_password` | Barcode Buddy web UI password | *(optional)* |

> **Note:** Automatic discovery only runs when `bbuddy_url` and `bbuddy_user` are both set.

---

## Home Assistant integration

The `custom_components/grocy_scraper/` directory contains a full Home Assistant integration that adds a **sidebar panel** to the HA UI, letting you search for products and discover barcodes from Barcode Buddy on a schedule — without ever touching the command line.

### Features

- **Sidebar panel** — type a search term and choose the number of results; see product names, EAN barcodes, and images in a responsive card grid
- **Config flow** — set up Grocy URL, API key, K-Ruoka store ID, and location / quantity-unit defaults through the HA UI
- **Options flow** — configure Barcode Buddy credentials and the **auto-discover interval** (periodic `--discover` run) directly from the HA settings page
- **WebSocket API** — the panel communicates with the backend via HA's native WebSocket interface; no extra ports or services needed

### Installation

1. Copy (or symlink) the `custom_components/grocy_scraper/` folder into your HA `config/custom_components/` directory.
2. Copy the `grocy_scraper/` Python package folder into the **same** `config/` directory so HA can import it:
   ```
   config/
   ├── custom_components/
   │   └── grocy_scraper/   ← HA integration
   └── grocy_scraper/       ← Python package (scraper + Grocy client)
   ```
3. Restart Home Assistant.
4. Navigate to **Settings → Devices & Services → Add Integration** and search for **Grocy Scraper**.
5. Follow the config flow to enter your credentials.
6. The **Grocy Scraper** entry appears in the HA sidebar.

### Settings (options flow)

Open the integration's **Configure** button to set:

| Setting | Description | Default |
|---------|-------------|---------|
| Barcode Buddy URL | Base URL of your Barcode Buddy instance | *(empty)* |
| Barcode Buddy API Key | API key for `/api` endpoints | *(empty)* |
| Barcode Buddy Username | Web UI login | *(empty)* |
| Barcode Buddy Password | Web UI password | *(empty)* |
| Auto-discover interval | Minutes between automatic `--discover` runs | `60` |
| Upload product images | Download and upload images when creating products | `true` |
| Use GraphQL backend | Use the faster GraphQL backend (recommended) | `true` |

> **Note:** Automatic discovery only runs when Barcode Buddy URL, username, and password are all configured.

---

## Features (CLI)

- Two backends – pick the one that works for you:
  - **GraphQL** (default) – `mobile.k-ruoka.fi/graphql`, no Cloudflare bypass needed
  - **kr-api REST** (fallback) – `www.k-ruoka.fi/kr-api`, requires a CF bypass
- Searches or browses the k-ruoka.fi product catalogue by keyword or full catalogue
- Handles both `Product` and `AssortmentSearchResult` GraphQL types
- Full category-based catalogue browsing via 27 hardcoded K-group category slugs
- Extracts product name and EAN/barcode for each item
- Creates products in Grocy and attaches their EAN barcodes
- Skips products whose barcode is already registered in Grocy (configurable)
- **Barcode Buddy integration** (`--discover`): fetches unknown barcodes from [Barcode Buddy](https://github.com/Forceu/barcodebuddy), searches K-Ruoka for matching products, adds them to Grocy, stocks them, and removes from the BB unknown list
- Supports `--dry-run` mode: scrape only, do not write to Grocy
- Configuration via CLI flags *or* environment variables / `.env` file

---

## Requirements

- Python 3.10+
- A running Grocy instance with a valid API key
- The K-group store ID for the store you want to scrape (see [Finding your store ID](#finding-your-store-id))
- **No Cloudflare bypass needed** when using the default GraphQL backend
- *(Optional)* A [Barcode Buddy](https://github.com/Forceu/barcodebuddy) instance for the `--discover` feature

## Development setup

```bash
git clone https://github.com/Glitchtit/grocy_scraper.git
cd grocy_scraper
pip install -r requirements.txt
```

> **Note:** The standalone CLI (`main.py`) has been removed. All functionality
> is available through the Home Assistant add-on and custom integration.
> The entry point is `grocy_scraper_addon/main.py`.

---

## Backends

### GraphQL (default, recommended)

The mobile app GraphQL API at `https://mobile.k-ruoka.fi/graphql` — **no Cloudflare bypass required**.

**Limitations**: The server enforces an `offset ≤ 1000` hard limit, so a single
query returns at most 1,100 products.  The `browse()` mode works around this by
iterating all 27 category slugs, fetching up to 1,100 products per category.
Very large categories (> 1,100 products) will be partially fetched.

### kr-api REST (fallback)

The web SPA REST API at `https://www.k-ruoka.fi/kr-api` — requires a Cloudflare
bypass.  Enable it in the add-on configuration by setting `use_graphql` to `false`.

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
BARCODEBDY_URL=https://bbuddy.example.com
BARCODEBDY_API=your_bbuddy_api_key_here
```

---

## Features

All features below are available through the add-on configuration and the HA
custom integration panel.

### Browse / search the catalogue

Configure the add-on with your `store_id` and Grocy credentials. The add-on
runs `--browse` automatically on the configured schedule.

### AI: assign products to locations (--sort)

Uses Gemini AI to read your Grocy product list and available locations, then
sets each product's storage location to the most appropriate one (e.g. dairy →
fridge, cleaning supplies → cleaning cabinet).  Requires a Gemini API key in
the add-on configuration.

### AI: set default best-before days (--date)

Uses Gemini AI to estimate typical best-before days for every product in your
Grocy database and updates the `default_best_before_days` field accordingly.

### Discover products from Barcode Buddy (--discover)

Fetches unknown barcodes from your [Barcode Buddy](https://github.com/Forceu/barcodebuddy)
instance, searches K-Ruoka for matching products by EAN, creates them in Grocy,
adds to stock, and removes them from the Barcode Buddy unknown list.

Configure via add-on options or environment variables / `.env`:

```dotenv
BARCODEBDY_URL=https://bbuddy.example.com
BARCODEBDY_API=your_bbuddy_api_key_here
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
│   ├── scraper.py              # k-ruoka.fi product scraper (GraphQL + kr-api backends)
│   ├── grocy_client.py         # Grocy REST API client
│   └── barcodebuddy_client.py  # Barcode Buddy client (for --discover)
├── custom_components/
│   └── grocy_scraper/    # Home Assistant integration
│       ├── __init__.py   # Setup, panel registration, periodic discover
│       ├── config_flow.py
│       ├── const.py
│       ├── manifest.json
│       ├── strings.json
│       ├── translations/en.json
│       ├── ws_api.py     # WebSocket search + config handlers
│       └── www/
│           └── panel.js  # Sidebar panel web component
├── tests/
│   ├── test_scraper.py
│   ├── test_grocy_client.py
│   ├── test_barcodebuddy_client.py
│   └── test_main.py
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
| `POST` | `/api/stock/products/{id}/add` | Add units to stock (used by --discover) |
| `GET`  | `/api/objects/products` | List all products |
| `GET`  | `/api/objects/product_barcodes` | List all registered barcodes |

Authentication is performed via the `GROCY-API-KEY` HTTP header.

---

## Legal notice

Scraping k-ruoka.fi is subject to their [terms of service](https://www.k-ruoka.fi).  
This tool is intended for personal use only.  The author is not affiliated with K-ryhmä or K-ruoka.fi.
