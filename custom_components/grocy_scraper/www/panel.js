/**
 * Grocy Scraper – Home Assistant sidebar panel.
 *
 * A custom HTML element that lets users search for Finnish grocery products
 * on k-ruoka.fi and preview results.  Configuration (discover interval, store
 * ID, Barcode Buddy status) is fetched from the backend via WebSocket.
 *
 * Registration:  customElements.define("grocy-scraper-panel", GrocyScraperPanel)
 * Panel JS URL:  /grocy_scraper_panel/panel.js
 */

class GrocyScraperPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._searching = false;
    this._initialized = false;
  }

  // HA sets this property whenever the hass object changes.
  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._render();
      this._loadConfig();
    }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          padding: 16px;
          background: var(--primary-background-color, #fafafa);
          min-height: 100vh;
          box-sizing: border-box;
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        }

        /* ── Header ── */
        .header {
          display: flex;
          align-items: center;
          gap: 12px;
          margin-bottom: 20px;
        }
        .header h1 {
          font-size: 1.5rem;
          font-weight: 400;
          margin: 0;
          color: var(--primary-text-color, #212121);
        }

        /* ── Cards ── */
        .card {
          background: var(--card-background-color, #fff);
          border-radius: 12px;
          padding: 20px;
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1));
          margin-bottom: 16px;
        }
        .card h2 {
          font-size: 0.95rem;
          font-weight: 500;
          margin: 0 0 14px;
          color: var(--primary-text-color, #212121);
        }

        /* ── Search form ── */
        .form-row {
          display: flex;
          gap: 12px;
          align-items: flex-end;
          flex-wrap: wrap;
        }
        .form-group {
          display: flex;
          flex-direction: column;
          gap: 5px;
        }
        .form-group.grow {
          flex: 1;
          min-width: 200px;
        }
        label {
          font-size: 0.8rem;
          color: var(--secondary-text-color, #757575);
          font-weight: 500;
          letter-spacing: 0.02em;
        }
        input[type="text"],
        input[type="number"] {
          padding: 10px 14px;
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 8px;
          background: var(--primary-background-color, #fafafa);
          color: var(--primary-text-color, #212121);
          font-size: 0.95rem;
          outline: none;
          transition: border-color 0.2s;
        }
        input[type="text"]:focus,
        input[type="number"]:focus {
          border-color: var(--primary-color, #03a9f4);
        }
        input[type="number"] {
          width: 90px;
        }
        .search-btn {
          padding: 10px 22px;
          background: var(--primary-color, #03a9f4);
          color: var(--text-primary-color, #fff);
          border: none;
          border-radius: 8px;
          font-size: 0.95rem;
          cursor: pointer;
          transition: opacity 0.2s;
          white-space: nowrap;
        }
        .search-btn:hover { opacity: 0.88; }
        .search-btn:disabled { opacity: 0.45; cursor: not-allowed; }

        /* ── Status line ── */
        .status {
          margin-top: 10px;
          min-height: 20px;
          font-size: 0.85rem;
          color: var(--secondary-text-color, #757575);
        }
        .error { color: var(--error-color, #db4437); }

        /* ── Spinner ── */
        .loader {
          display: inline-block;
          width: 14px;
          height: 14px;
          border: 2px solid var(--divider-color, #e0e0e0);
          border-top-color: var(--primary-color, #03a9f4);
          border-radius: 50%;
          animation: spin 0.7s linear infinite;
          vertical-align: middle;
          margin-right: 6px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* ── Results grid ── */
        .results {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
          gap: 12px;
        }
        .product-card {
          background: var(--card-background-color, #fff);
          border-radius: 12px;
          padding: 14px;
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
          display: flex;
          gap: 12px;
          align-items: flex-start;
        }
        .product-img {
          width: 60px;
          height: 60px;
          object-fit: contain;
          border-radius: 8px;
          background: var(--secondary-background-color, #f5f5f5);
          flex-shrink: 0;
        }
        .product-placeholder {
          width: 60px;
          height: 60px;
          background: var(--secondary-background-color, #f5f5f5);
          border-radius: 8px;
          display: flex;
          align-items: center;
          justify-content: center;
          font-size: 1.6rem;
          flex-shrink: 0;
        }
        .product-info { flex: 1; min-width: 0; }
        .product-name {
          font-weight: 500;
          color: var(--primary-text-color, #212121);
          margin-bottom: 4px;
          word-break: break-word;
          line-height: 1.3;
        }
        .product-ean {
          font-size: 0.72rem;
          color: var(--secondary-text-color, #757575);
          font-family: monospace;
          letter-spacing: 0.05em;
        }
        .product-desc {
          font-size: 0.78rem;
          color: var(--secondary-text-color, #757575);
          margin-top: 4px;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }
        .no-results {
          text-align: center;
          color: var(--secondary-text-color, #757575);
          padding: 32px 16px;
          grid-column: 1 / -1;
          font-size: 0.9rem;
        }

        /* ── Config info ── */
        .config-info {
          font-size: 0.85rem;
          color: var(--secondary-text-color, #757575);
          line-height: 1.7;
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
        }
        .badge {
          display: inline-flex;
          align-items: center;
          padding: 3px 10px;
          border-radius: 12px;
          background: var(--primary-color, #03a9f4);
          color: var(--text-primary-color, #fff);
          font-size: 0.75rem;
          font-weight: 600;
          gap: 4px;
        }
        .badge.ok   { background: var(--success-color, #4caf50); }
        .badge.warn { background: var(--warning-color, #ff9800); }
      </style>

      <!-- Header -->
      <div class="header">
        <h1>🛒 Grocy Scraper</h1>
      </div>

      <!-- Search card -->
      <div class="card">
        <h2>Search for products on K-Ruoka</h2>
        <div class="form-row">
          <div class="form-group grow">
            <label for="query">Search term</label>
            <input
              type="text"
              id="query"
              placeholder="e.g. maito, leipä, juusto …"
              autocomplete="off"
            />
          </div>
          <div class="form-group">
            <label for="max-products">Max results</label>
            <input type="number" id="max-products" value="50" min="1" max="500" />
          </div>
          <button class="search-btn" id="search-btn">Search</button>
        </div>
        <div class="status" id="status"></div>
      </div>

      <!-- Results -->
      <div class="results" id="results"></div>

      <!-- Config info card (hidden until loaded) -->
      <div class="card" id="config-card" style="display:none">
        <h2>⚙️ Integration settings</h2>
        <div class="config-info" id="config-info"></div>
      </div>
    `;

    // Event listeners
    this.shadowRoot
      .querySelector("#search-btn")
      .addEventListener("click", () => this._search());

    this.shadowRoot
      .querySelector("#query")
      .addEventListener("keydown", (e) => {
        if (e.key === "Enter") this._search();
      });
  }

  // -------------------------------------------------------------------------
  // Load current config from backend
  // -------------------------------------------------------------------------

  async _loadConfig() {
    if (!this._hass) return;
    try {
      const cfg = await this._hass.callWS({ type: "grocy_scraper/get_config" });
      if (!cfg.configured) return;

      const card = this.shadowRoot.querySelector("#config-card");
      const info = this.shadowRoot.querySelector("#config-info");
      card.style.display = "";

      const discoverBadge = cfg.bbuddy_configured
        ? `<span class="badge ok">🔄 Auto-discover every ${cfg.discover_interval} min</span>`
        : `<span class="badge warn">⚠️ Barcode Buddy not configured</span>`;

      info.innerHTML = `
        <span class="badge">Store: ${cfg.store_id || "—"}</span>
        ${discoverBadge}
      `;
    } catch (err) {
      // "not_found" / unregistered WS type means the integration isn't set up yet
      // – that is expected; any other error is worth noting in the console.
      if (!err || err.code !== "not_found") {
        console.debug("[grocy_scraper] get_config failed:", err);
      }
    }
  }

  // -------------------------------------------------------------------------
  // Search
  // -------------------------------------------------------------------------

  async _search() {
    if (!this._hass || this._searching) return;

    const query = this.shadowRoot.querySelector("#query").value.trim();
    if (!query) return;

    const maxProducts =
      parseInt(this.shadowRoot.querySelector("#max-products").value, 10) || 50;

    const statusEl = this.shadowRoot.querySelector("#status");
    const resultsEl = this.shadowRoot.querySelector("#results");
    const btn = this.shadowRoot.querySelector("#search-btn");

    this._searching = true;
    btn.disabled = true;
    statusEl.innerHTML = `<span class="loader"></span>Searching for "${query}" …`;
    resultsEl.innerHTML = "";

    try {
      const result = await this._hass.callWS({
        type: "grocy_scraper/search",
        query,
        max_products: maxProducts,
      });

      const products = result.products || [];
      statusEl.textContent =
        products.length > 0
          ? `Found ${products.length} product(s).`
          : `No products found for "${query}".`;

      this._renderResults(products, resultsEl);
    } catch (err) {
      const msg = (err && err.message) || String(err);
      const span = document.createElement("span");
      span.className = "error";
      span.textContent = `Error: ${msg}`;
      statusEl.textContent = "";
      statusEl.appendChild(span);
    } finally {
      this._searching = false;
      btn.disabled = false;
    }
  }

  // -------------------------------------------------------------------------
  // Results rendering
  // -------------------------------------------------------------------------

  _renderResults(products, container) {
    if (!products.length) {
      container.innerHTML =
        '<div class="no-results">No products found. Try a different search term.</div>';
      return;
    }

    container.innerHTML = products
      .map((p) => {
        const img = p.image_url
          ? `<img class="product-img" src="${this._escape(p.image_url)}" alt="${this._escape(p.name)}" loading="lazy" />`
          : `<div class="product-placeholder">📦</div>`;
        const desc = p.description
          ? `<div class="product-desc">${this._escape(p.description)}</div>`
          : "";
        return `
          <div class="product-card">
            ${img}
            <div class="product-info">
              <div class="product-name">${this._escape(p.name)}</div>
              <div class="product-ean">${this._escape(p.ean || "—")}</div>
              ${desc}
            </div>
          </div>`;
      })
      .join("");
  }

  // Simple HTML escaping to prevent XSS from product data
  _escape(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}

customElements.define("grocy-scraper-panel", GrocyScraperPanel);
