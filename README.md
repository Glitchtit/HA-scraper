<p align="center">
  <img src="https://raw.githubusercontent.com/Glitchtit/HA-scraper/main/addon/logo.png" alt="Scraper" height="72">
</p>

<p align="center">
  <b>Fills Storage with Finnish grocery products from k-ruoka.fi</b><br>
  Part of Glitchtit's HA apps — needs the Storage add-on.
</p>

<p align="center">
  <a href="https://github.com/Glitchtit/HA-apps"><img alt="Home Assistant add-on" src="https://img.shields.io/badge/Home%20Assistant-add--on-0047AB?style=flat-square&logo=homeassistant&logoColor=white"></a>
  <img alt="Version" src="https://img.shields.io/badge/dynamic/yaml?style=flat-square&color=FF4F00&label=version&url=https%3A%2F%2Fraw.githubusercontent.com%2FGlitchtit%2FHA-scraper%2Fmain%2Faddon%2Fconfig.yaml&query=%24.version">
  <a href="https://hacs.xyz"><img alt="HACS custom" src="https://img.shields.io/badge/HACS-custom-1F2937?style=flat-square"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/Glitchtit/HA-scraper/main/docs/screenshots/hero.png" alt="Scraper screenshot" width="100%">
</p>

## ✨ Features

- **Product search** — search K-Ruoka by name, tick the results you want and add them to Storage with name, EAN barcode and product image. Barcodes already in Storage are skipped.
- **Barcode queue discovery** — **Discover** works through the pending barcodes in Storage's queue: it looks each one up on K-Ruoka, then s-kaupat.fi, then SearXNG if configured, creates the product, adds it to stock and marks the queue item done.
- **Catalogue update** — **Update** re-checks every Storage product by barcode and refreshes its name, image and price. Names found only through SearXNG fill in placeholder names but never overwrite a real one.
- **Prices and per-store availability** — every configured store is checked per product; whether the store carries it is written to Storage, and the store's regular price is saved as the product's `unit_price` (EUR). Prices come from Cloudflare-protected kr-api, so they need FlareSolverr and are skipped without it.
- **Rate-limit protection** — availability uses the mobile GraphQL API, which is not throttled. kr-api price lookups are limited to 200 products per run (oldest first, skipping prices refreshed in the last 3 days), paced with jitter, paused for 15 minutes after repeated 429s, and reuse a cached Cloudflare clearance.
- **Finds Storage by itself** — the Storage add-on is detected from the container hostname or the Supervisor API, and the scraper keeps retrying while Storage is down.

## 🚀 Installation

### Add-on

1. In Home Assistant open **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add `https://github.com/Glitchtit/HA-apps`
2. Install **Storage** first, then **Scraper**
3. In the **Configuration** tab set `store_id` to your K-group store ID(s); add `flaresolverr_url` if you want prices
4. Start it and open **Scraper** from the sidebar

### Integration (HACS)

1. In HACS open **⋮ → Custom repositories**, add `https://github.com/Glitchtit/HA-scraper` with category **Integration**
2. Download **Scraper** and restart Home Assistant
3. **Settings → Devices & services → Add integration → Scraper** (enter the Storage URL and your K-group store ID; the integration talks to Storage and K-Ruoka directly)

## ⚙️ Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `storage_hostname` | `""` | Hostname of the Storage add-on (reached on port `8099`). Leave empty to auto-detect. A full `storage_url` is also accepted in the schema and takes priority. |
| `store_id` | `K532` | K-group store ID, or several comma-separated (e.g. `K532,N110`). Search tries them in order; availability is checked in all of them. |
| `upload_images` | `true` | Download product images and upload them to Storage |
| `use_graphql` | `true` | Use the mobile GraphQL API for name and image lookups. Set `false` to use kr-api, which needs a Cloudflare bypass. |
| `flaresolverr_url` | `""` | FlareSolverr address (with or without `/v1`). Needed for prices. |
| `searxng_url` | `""` | SearXNG instance used as a last-resort barcode lookup |

## 🏠 In Home Assistant

The integration adds no entities and no sidebar panel (the add-on's panel is the UI). It registers two actions:

| Action | What it does |
|--------|--------------|
| `scraper.search_products` | Search K-Ruoka by name (`query`, optional `max_products`, 1–500, default 50) |
| `scraper.add_product` | Create a product in Storage from a `name` with optional `ean`, `description` and `image_url` (the image is downloaded and uploaded) |

`search_products` returns `{products: [{name, ean, description, image_url}]}`; `add_product` can return `{product_id}`.

## 🧩 The family

<p align="center">
  <a href="https://github.com/Glitchtit/HA-storage"><img src="https://raw.githubusercontent.com/Glitchtit/HA-storage/main/storage/icon.png" width="56" alt="Storage"></a>&nbsp;
  <a href="https://github.com/Glitchtit/HA-scraper"><img src="https://raw.githubusercontent.com/Glitchtit/HA-scraper/main/addon/icon.png" width="56" alt="Scraper"></a>&nbsp;
  <a href="https://github.com/Glitchtit/HA-stock"><img src="https://raw.githubusercontent.com/Glitchtit/HA-stock/main/stock/icon.png" width="56" alt="Stock"></a>&nbsp;
  <a href="https://github.com/Glitchtit/HA-recipes"><img src="https://raw.githubusercontent.com/Glitchtit/HA-recipes/main/recipes/icon.png" width="56" alt="Recipes"></a>&nbsp;
  <a href="https://github.com/Glitchtit/HA-chores"><img src="https://raw.githubusercontent.com/Glitchtit/HA-chores/main/chores/icon.png" width="56" alt="Chores"></a>&nbsp;
  <a href="https://github.com/Glitchtit/HA-lists"><img src="https://raw.githubusercontent.com/Glitchtit/HA-lists/main/lists/icon.png" width="56" alt="Lists"></a>&nbsp;
  <a href="https://github.com/Glitchtit/HA-print"><img src="https://raw.githubusercontent.com/Glitchtit/HA-print/main/print/icon.png" width="56" alt="Print"></a>
</p>

<p align="center"><sub>Scraper writes only to Storage; Stock's quick-add and Storage's product finder call it to look up and add K-Ruoka products.</sub></p>

## 🛠️ Development

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

Tests import the top-level `scraper/` package. `addon/scraper/` and `custom_components/scraper/scraperlib/` are copies of it and must be kept in sync by hand.
