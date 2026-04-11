# grocy_scraper

A **[Home Assistant](https://www.home-assistant.io/) custom integration** and **Home Assistant Supervisor add-on** that scrapes **[k-ruoka.fi](https://www.k-ruoka.fi/kauppa)** for Finnish food products and their EAN barcodes, then stores them in **[HA-Storage](https://github.com/Glitchtit/HA-storage)** — the central data store for this ecosystem.

---

## Home Assistant Supervisor add-on

The `grocy_scraper_addon/` directory contains a full Home Assistant Supervisor add-on that runs the scraper in a dedicated Docker container managed by HA Supervisor.

### Add-on installation

1. In Home Assistant, navigate to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** (three-dot) menu in the top-right and choose **Repositories**.
3. Add `https://github.com/Glitchtit/HA-apps` and click **Add**.
4. The **Grocy Scraper** add-on will appear in the store — click **Install**.
5. Open the add-on's **Configuration** tab and fill in at minimum:
   - `storage_url` — base URL of your HA-Storage instance (e.g. `http://a0a9ed235_ha_storage:8099`)
   - `store_id` — K-group store ID (see [Finding your store ID](#finding-your-store-id))
6. Click **Save** and then **Start**.

### Add-on options

| Option | Description | Default |
|--------|-------------|---------|
| `storage_url` | Base URL of your HA-Storage instance | *(required)* |
| `store_id` | K-group store ID (e.g. `N110`) | `N110` |
| `upload_images` | Download and upload product images | `true` |
| `use_graphql` | Use the faster GraphQL backend (recommended) | `true` |
| `gemini_api_key` | Google Gemini API key (for Sort, Date, Group, Optimize) | *(optional)* |
| `gemini_model` | Gemini model name | `gemini-1.5-flash` |

---

## Home Assistant integration

The `custom_components/grocy_scraper/` directory contains a full Home Assistant integration that adds a **sidebar panel** to the HA UI, letting you search for products and trigger AI-powered actions — without ever touching the command line.

### Features

- **Sidebar panel** — search k-ruoka.fi by keyword; see product names, EAN barcodes, and images in a responsive card grid
- **Config flow** — set up Storage URL and K-Ruoka store ID through the HA UI
- **Options flow** — configure Gemini AI settings directly from the HA settings page
- **AI actions** — Sort (assign locations), Date (set best-before days), Group (group products), Optimize (full AI pass)
- **Auto-discover** — periodic scan of the Storage barcode queue, looking up unknown barcodes on K-Ruoka and creating products automatically
- **WebSocket API** — the panel communicates with the backend via HA's native WebSocket interface; no extra ports or services needed

### Installation

1. Copy (or symlink) the `custom_components/grocy_scraper/` folder into your HA `config/custom_components/` directory.
2. Copy the `grocy_scraper/` Python package folder into the **same** `config/` directory so HA can import it:
   ```
   config/
   ├── custom_components/
   │   └── grocy_scraper/   ← HA integration
   └── grocy_scraper/       ← Python package (scraper + Storage client)
   ```
3. Restart Home Assistant.
4. Navigate to **Settings → Devices & Services → Add Integration** and search for **Grocy Scraper**.
5. Follow the config flow to enter your Storage URL and store ID.
6. The **Grocy Scraper** entry appears in the HA sidebar.

### Settings (options flow)

Open the integration's **Configure** button to set:

| Setting | Description | Default |
|---------|-------------|---------|
| Upload product images | Download and upload images when creating products | `true` |
| Use GraphQL backend | Use the faster GraphQL backend (recommended) | `true` |
| Gemini API Key | Google Gemini API key for AI actions | *(empty)* |
| Gemini model name | e.g. `gemini-1.5-flash` | `gemini-1.5-flash` |
| Gemini model (Optimize) | Override model for the Optimize action | *(uses default)* |

---

## Backends

### GraphQL (default, recommended)

The mobile app GraphQL API at `https://mobile.k-ruoka.fi/graphql` — **no Cloudflare bypass required**.

**Limitations**: The server enforces an `offset ≤ 1000` hard limit.  The `browse()` mode works around this by iterating all category slugs.

### kr-api REST (fallback)

The web SPA REST API at `https://www.k-ruoka.fi/kr-api` — requires a Cloudflare bypass.  Enable it by setting `use_graphql` to `false`.

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
│   ├── storage_client.py       # HA-Storage REST API client
│   └── skaupat_client.py       # S-kaupat.fi EAN lookup
├── grocy_scraper_addon/
│   ├── grocy_scraper/          # Mirror of the package above (kept in sync)
│   ├── main.py                 # Add-on entry point + Gemini AI helpers
│   └── ingress_server.py       # HA ingress web server
├── custom_components/
│   └── grocy_scraper/          # Home Assistant integration
│       ├── __init__.py
│       ├── config_flow.py
│       ├── const.py
│       ├── manifest.json
│       ├── strings.json
│       ├── translations/en.json
│       ├── ws_api.py           # WebSocket API handlers
│       └── www/
│           └── panel.js        # Sidebar panel web component
└── tests/
```

---

## Running tests

```bash
cd grocy_scraper
python -m pytest tests/ -v
```

---

## Legal notice

Scraping k-ruoka.fi is subject to their [terms of service](https://www.k-ruoka.fi).  
This tool is intended for personal use only.  The author is not affiliated with K-ryhmä or K-ruoka.fi.

