"""WebSocket API handlers for the Grocy Scraper integration."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    DOMAIN,
    CONF_STORE_ID,
    CONF_USE_GRAPHQL,
    DEFAULT_USE_GRAPHQL,
)

_LOGGER = logging.getLogger(__name__)

# Repo root is three levels up: custom_components/grocy_scraper/ → repo root
_REPO_ROOT = Path(__file__).parent.parent.parent


def _ensure_repo_on_path() -> None:
    """Add the repository root to sys.path so the grocy_scraper package is importable."""
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register WebSocket commands exposed by this integration."""
    websocket_api.async_register_command(hass, ws_search_products)
    websocket_api.async_register_command(hass, ws_get_config)


# ---------------------------------------------------------------------------
# grocy_scraper/search
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "grocy_scraper/search",
        vol.Required("query"): str,
        vol.Optional("max_products", default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=500)
        ),
    }
)
@websocket_api.async_response
async def ws_search_products(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle a product-search request from the sidebar panel.

    Returns a list of products matching the query, each with ``name``,
    ``ean``, ``description``, and ``image_url`` fields.
    """
    # Locate the first (and usually only) config entry.
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        connection.send_error(
            msg["id"], "not_configured", "Grocy Scraper is not configured."
        )
        return

    entry = entries[0]
    store_id: str = entry.data.get(CONF_STORE_ID, "")
    use_graphql: bool = entry.options.get(CONF_USE_GRAPHQL, DEFAULT_USE_GRAPHQL)

    query: str = msg["query"]
    max_products: int = msg["max_products"]

    try:
        products = await hass.async_add_executor_job(
            _search_products_sync,
            store_id,
            use_graphql,
            query,
            max_products,
        )
        connection.send_result(msg["id"], {"products": products})
    except (OSError, ValueError, RuntimeError) as exc:
        _LOGGER.error("Product search failed: %s", exc)
        connection.send_error(msg["id"], "search_failed", str(exc))
    except Exception as exc:  # noqa: BLE001 – catch-all so the WS connection stays open
        _LOGGER.exception("Unexpected error during product search")
        connection.send_error(msg["id"], "search_failed", str(exc))


def _search_products_sync(
    store_id: str,
    use_graphql: bool,
    query: str,
    max_products: int,
) -> list[dict[str, str]]:
    """Run a synchronous K-Ruoka product search and return serialisable dicts."""
    _ensure_repo_on_path()
    from grocy_scraper.scraper import KRuokaScraper  # noqa: PLC0415

    scraper = KRuokaScraper(store_id=store_id, use_graphql=use_graphql)
    results: list[dict[str, str]] = []
    for product in scraper.search(query, max_products=max_products):
        results.append(
            {
                "name": product.name,
                "ean": product.ean or "",
                "description": product.description or "",
                "image_url": product.image_url or "",
            }
        )
    return results


# ---------------------------------------------------------------------------
# grocy_scraper/get_config  (used by the panel to show current settings)
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "grocy_scraper/get_config",
    }
)
@websocket_api.async_response
async def ws_get_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a subset of the current config to the sidebar panel."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        connection.send_result(msg["id"], {"configured": False})
        return

    entry = entries[0]
    from .const import CONF_DISCOVER_INTERVAL, DEFAULT_DISCOVER_INTERVAL  # noqa: PLC0415

    connection.send_result(
        msg["id"],
        {
            "configured": True,
            "store_id": entry.data.get(CONF_STORE_ID, ""),
            "discover_interval": entry.options.get(
                CONF_DISCOVER_INTERVAL, DEFAULT_DISCOVER_INTERVAL
            ),
            "bbuddy_configured": bool(
                entry.options.get("bbuddy_url")
                and entry.options.get("bbuddy_user")
                and entry.options.get("bbuddy_password")
            ),
        },
    )
