/**
 * Grocy Scraper – Home Assistant sidebar panel.
 *
 * Features
 * --------
 * • Search for K-Ruoka products by keyword (configurable max results)
 * • Action buttons: Discover, Sort, Date
 * • Terminal output pane that shows logs from each operation
 *   – Verbose toggle shows/hides DEBUG-level lines
 *   – Clear button empties the terminal
 * • Settings info card (store ID, discover interval, feature status)
 */

class GrocyScraperPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._searching = false;
    this._running = false;   // true while any action button is in progress
    this._initialized = false;
    this._verbose = false;
    this._logSessions = [];  // [{label, logs}] – cumulative terminal history
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._render();
      this._loadConfig();
    }
  }

  // -------------------------------------------------------------------------
  // Render (initial full DOM build)
  // -------------------------------------------------------------------------

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          padding: 16px;
          background: var(--primary-background-color, #121212);
          min-height: 100vh;
          box-sizing: border-box;
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        }

        /* ── Header ── */
        .header { display:flex; align-items:center; gap:12px; margin-bottom:20px; }
        .header h1 {
          font-size:1.5rem; font-weight:400; margin:0;
          color:var(--primary-text-color,#e0e0e0);
        }

        /* ── Cards ── */
        .card {
          background:var(--card-background-color,#1e1e1e);
          border-radius:12px; padding:20px;
          box-shadow:var(--ha-card-box-shadow,0 2px 4px rgba(0,0,0,.4));
          margin-bottom:16px;
        }
        .card h2 {
          font-size:0.95rem; font-weight:500; margin:0 0 14px;
          color:var(--primary-text-color,#e0e0e0);
        }

        /* ── Forms ── */
        .form-row { display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap; }
        .form-group { display:flex; flex-direction:column; gap:5px; }
        .form-group.grow { flex:1; min-width:200px; }
        label {
          font-size:0.8rem; color:var(--secondary-text-color,#9e9e9e);
          font-weight:500; letter-spacing:0.02em;
        }
        input[type="text"], input[type="number"] {
          padding:10px 14px;
          border:1px solid var(--divider-color,#444);
          border-radius:8px;
          background:var(--primary-background-color,#2a2a2a);
          color:var(--primary-text-color,#e0e0e0);
          font-size:0.95rem; outline:none; transition:border-color 0.2s;
        }
        input[type="text"]:focus, input[type="number"]:focus {
          border-color:var(--primary-color,#03a9f4);
        }
        input[type="number"] { width:90px; }

        /* ── Buttons (shared base) ── */
        .btn {
          padding:10px 20px; border:none; border-radius:8px;
          font-size:0.9rem; cursor:pointer; transition:opacity 0.2s;
          white-space:nowrap; font-weight:500;
        }
        .btn:hover { opacity:0.88; }
        .btn:disabled { opacity:0.4; cursor:not-allowed; }
        .btn-primary {
          background:var(--primary-color,#03a9f4);
          color:var(--text-primary-color,#fff);
        }
        .btn-discover { background:#6200ea; color:#fff; }
        .btn-sort     { background:#00796b; color:#fff; }
        .btn-date     { background:#e65100; color:#fff; }
        .btn-sm {
          padding:5px 12px; font-size:0.78rem; border-radius:6px;
          background:var(--secondary-background-color,#333);
          color:var(--secondary-text-color,#bbb);
          border:1px solid var(--divider-color,#555);
        }

        /* ── Status lines ── */
        .status {
          margin-top:10px; min-height:20px;
          font-size:0.85rem; color:var(--secondary-text-color,#9e9e9e);
        }
        .error   { color:var(--error-color,#f44747); }
        .success { color:var(--success-color,#4caf50); }

        /* ── Spinner ── */
        .loader {
          display:inline-block; width:14px; height:14px;
          border:2px solid var(--divider-color,#444);
          border-top-color:var(--primary-color,#03a9f4);
          border-radius:50%;
          animation:spin 0.7s linear infinite;
          vertical-align:middle; margin-right:6px;
        }
        @keyframes spin { to { transform:rotate(360deg); } }

        /* ── Results grid ── */
        .results {
          display:grid;
          grid-template-columns:repeat(auto-fill,minmax(270px,1fr));
          gap:12px;
        }
        .product-card {
          background:var(--card-background-color,#1e1e1e);
          border-radius:12px; padding:14px;
          box-shadow:var(--ha-card-box-shadow,0 2px 4px rgba(0,0,0,.4));
          display:flex; gap:12px; align-items:flex-start;
        }
        .product-img {
          width:60px; height:60px; object-fit:contain; border-radius:8px;
          background:var(--secondary-background-color,#2a2a2a); flex-shrink:0;
        }
        .product-placeholder {
          width:60px; height:60px;
          background:var(--secondary-background-color,#2a2a2a);
          border-radius:8px; display:flex; align-items:center;
          justify-content:center; font-size:1.6rem; flex-shrink:0;
        }
        .product-info { flex:1; min-width:0; }
        .product-name {
          font-weight:500; color:var(--primary-text-color,#e0e0e0);
          margin-bottom:4px; word-break:break-word; line-height:1.3;
        }
        .product-ean {
          font-size:0.72rem; color:var(--secondary-text-color,#9e9e9e);
          font-family:monospace; letter-spacing:0.05em;
        }
        .product-desc {
          font-size:0.78rem; color:var(--secondary-text-color,#9e9e9e);
          margin-top:4px;
          display:-webkit-box; -webkit-line-clamp:2;
          -webkit-box-orient:vertical; overflow:hidden;
        }
        .no-results {
          text-align:center; color:var(--secondary-text-color,#9e9e9e);
          padding:32px 16px; grid-column:1/-1; font-size:0.9rem;
        }

        /* ── Action buttons row ── */
        .action-row { display:flex; gap:10px; flex-wrap:wrap; }

        /* ── Terminal card ── */
        .terminal-header {
          display:flex; align-items:center; gap:10px;
          margin-bottom:10px; flex-wrap:wrap;
        }
        .terminal-header h2 { margin:0; flex:1; min-width:120px; }
        .verbose-label {
          display:inline-flex; align-items:center; gap:6px;
          font-size:0.82rem; color:var(--secondary-text-color,#9e9e9e);
          cursor:pointer; user-select:none;
        }
        .verbose-label input[type="checkbox"] { cursor:pointer; }
        .terminal {
          background:#0d0d0d; color:#d4d4d4;
          border-radius:8px; padding:14px;
          font-family:'Courier New',Courier,monospace; font-size:0.78rem;
          line-height:1.55; overflow-y:auto; max-height:400px;
          white-space:pre-wrap; word-break:break-all;
          scrollbar-width:thin; scrollbar-color:#555 #0d0d0d;
        }
        .terminal:empty::before {
          content:"No output yet. Run an action above.";
          color:#555; font-style:italic;
        }
        /* Log-level colours */
        .log-debug    { color:#858585; }
        .log-info     { color:#d4d4d4; }
        .log-warning  { color:#ffcc00; }
        .log-error    { color:#f44747; }
        .log-critical { color:#f44747; font-weight:bold; }
        /* Session separator */
        .log-session-head {
          color:#569cd6; font-weight:bold; margin-top:6px;
          border-top:1px solid #333; padding-top:4px;
        }
        /* Hide DEBUG lines when verbose is OFF */
        .terminal:not(.verbose) .log-debug { display:none; }

        /* ── Config info badges ── */
        .config-info {
          font-size:0.85rem; color:var(--secondary-text-color,#9e9e9e);
          display:flex; flex-wrap:wrap; gap:8px;
        }
        .badge {
          display:inline-flex; align-items:center; padding:3px 10px;
          border-radius:12px; background:var(--primary-color,#03a9f4);
          color:var(--text-primary-color,#fff); font-size:0.75rem;
          font-weight:600; gap:4px;
        }
        .badge.ok   { background:var(--success-color,#4caf50); }
        .badge.warn { background:var(--warning-color,#ff9800); }
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
            <input type="text" id="query"
              placeholder="e.g. maito, leipä, juusto …" autocomplete="off" />
          </div>
          <div class="form-group">
            <label for="max-products">Max results</label>
            <input type="number" id="max-products" value="50" min="1" max="500" />
          </div>
          <button class="btn btn-primary" id="search-btn">Search</button>
        </div>
        <div class="status" id="search-status"></div>
      </div>

      <!-- Search results -->
      <div class="results" id="results"></div>

      <!-- Actions card -->
      <div class="card">
        <h2>🔧 Actions</h2>
        <div class="action-row">
          <button class="btn btn-discover" id="discover-btn">🔍 Discover</button>
          <button class="btn btn-sort"     id="sort-btn">📦 Sort</button>
          <button class="btn btn-date"     id="date-btn">📅 Date</button>
        </div>
        <div class="status" id="action-status"></div>
      </div>

      <!-- Terminal output card -->
      <div class="card" id="terminal-card">
        <div class="terminal-header">
          <h2>📟 Output</h2>
          <label class="verbose-label">
            <input type="checkbox" id="verbose-toggle" />
            Verbose
          </label>
          <button class="btn btn-sm" id="clear-btn">Clear</button>
        </div>
        <div class="terminal" id="terminal"></div>
      </div>

      <!-- Config info card (hidden until loaded) -->
      <div class="card" id="config-card" style="display:none">
        <h2>⚙️ Integration settings</h2>
        <div class="config-info" id="config-info"></div>
      </div>
    `;

    // ── Search ──
    this.shadowRoot.querySelector("#search-btn")
      .addEventListener("click", () => this._search());
    this.shadowRoot.querySelector("#query")
      .addEventListener("keydown", (e) => { if (e.key === "Enter") this._search(); });

    // ── Actions ──
    this.shadowRoot.querySelector("#discover-btn")
      .addEventListener("click", () => this._runAction("grocy_scraper/run_discover", "Discover"));
    this.shadowRoot.querySelector("#sort-btn")
      .addEventListener("click", () => this._runAction("grocy_scraper/run_sort", "Sort"));
    this.shadowRoot.querySelector("#date-btn")
      .addEventListener("click", () => this._runAction("grocy_scraper/run_date", "Date"));

    // ── Terminal controls ──
    this.shadowRoot.querySelector("#verbose-toggle")
      .addEventListener("change", (e) => {
        this._verbose = e.target.checked;
        const terminal = this.shadowRoot.querySelector("#terminal");
        terminal.classList.toggle("verbose", this._verbose);
      });
    this.shadowRoot.querySelector("#clear-btn")
      .addEventListener("click", () => {
        this._logSessions = [];
        this.shadowRoot.querySelector("#terminal").innerHTML = "";
      });
  }

  // -------------------------------------------------------------------------
  // Load config badges
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

      const geminiBadge = cfg.gemini_configured
        ? `<span class="badge ok">🤖 Gemini AI ready</span>`
        : `<span class="badge warn">⚠️ Gemini API key not set</span>`;

      info.innerHTML = `
        <span class="badge">Store: ${this._escape(cfg.store_id || "—")}</span>
        ${discoverBadge}
        ${geminiBadge}
      `;
    } catch (err) {
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

    const statusEl = this.shadowRoot.querySelector("#search-status");
    const resultsEl = this.shadowRoot.querySelector("#results");
    const btn = this.shadowRoot.querySelector("#search-btn");

    this._searching = true;
    btn.disabled = true;
    statusEl.innerHTML = `<span class="loader"></span>Searching for "${this._escape(query)}" …`;
    resultsEl.innerHTML = "";

    try {
      const result = await this._hass.callWS({
        type: "grocy_scraper/search",
        query,
        max_products: maxProducts,
      });

      const products = result.products || [];
      statusEl.textContent = products.length > 0
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
  // Action runner (Discover / Sort / Date)
  // -------------------------------------------------------------------------

  async _runAction(wsType, label) {
    if (!this._hass || this._running) return;

    const statusEl   = this.shadowRoot.querySelector("#action-status");
    const discoverBtn = this.shadowRoot.querySelector("#discover-btn");
    const sortBtn     = this.shadowRoot.querySelector("#sort-btn");
    const dateBtn     = this.shadowRoot.querySelector("#date-btn");

    this._running = true;
    discoverBtn.disabled = sortBtn.disabled = dateBtn.disabled = true;
    statusEl.innerHTML = `<span class="loader"></span>Running ${this._escape(label)} …`;

    try {
      const result = await this._hass.callWS({ type: wsType });

      // Append logs to the terminal pane
      if (result.logs && result.logs.length) {
        this._appendLogs(label, result.logs);
      }

      // Show summary status
      if (result.skipped) {
        statusEl.innerHTML =
          `<span class="error">${this._escape(label)}: not configured — see terminal output.</span>`;
      } else if (result.success !== true) {
        statusEl.innerHTML =
          `<span class="error">${this._escape(label)} finished with errors — see terminal output.</span>`;
      } else {
        const extra = result.updated != null ? ` (${result.updated} updated)` : "";
        statusEl.innerHTML =
          `<span class="success">✓ ${this._escape(label)} complete${this._escape(extra)}.</span>`;
      }
    } catch (err) {
      const msg = (err && err.message) || String(err);
      // Show as an error log line in terminal too
      this._appendLogs(label, [{ level: "ERROR", message: msg }]);
      statusEl.innerHTML =
        `<span class="error">Error running ${this._escape(label)}: ${this._escape(msg)}</span>`;
    } finally {
      this._running = false;
      discoverBtn.disabled = sortBtn.disabled = dateBtn.disabled = false;
    }
  }

  // -------------------------------------------------------------------------
  // Terminal helpers
  // -------------------------------------------------------------------------

  _appendLogs(label, logs) {
    const terminal = this.shadowRoot.querySelector("#terminal");
    const now = new Date().toLocaleTimeString();

    // Session heading
    const heading = document.createElement("div");
    heading.className = "log-session-head";
    heading.textContent = `▶ ${label}  [${now}]`;
    terminal.appendChild(heading);

    // Log lines
    for (const entry of logs) {
      const line = document.createElement("div");
      const lvl = (entry.level || "INFO").toUpperCase();
      line.className = `log-${lvl.toLowerCase()}`;
      line.setAttribute("data-level", lvl);
      line.textContent = entry.message;
      terminal.appendChild(line);
    }

    // Apply current verbose state
    terminal.classList.toggle("verbose", this._verbose);

    // Auto-scroll to bottom
    terminal.scrollTop = terminal.scrollHeight;
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

    container.innerHTML = products.map((p) => {
      const img = p.image_url
        ? `<img class="product-img" src="${this._escape(p.image_url)}"
               alt="${this._escape(p.name)}" loading="lazy" />`
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
    }).join("");
  }

  // -------------------------------------------------------------------------
  // Utility
  // -------------------------------------------------------------------------

  _escape(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}

customElements.define("grocy-scraper-panel", GrocyScraperPanel);
