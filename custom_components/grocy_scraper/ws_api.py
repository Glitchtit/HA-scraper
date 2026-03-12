"""WebSocket API handlers for the Grocy Scraper integration."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    DOMAIN,
    CONF_GROCY_URL,
    CONF_GROCY_KEY,
    CONF_STORE_ID,
    CONF_LOCATION_ID,
    CONF_QUANTITY_UNIT_ID,
    CONF_BBUDDY_URL,
    CONF_BBUDDY_KEY,
    CONF_BBUDDY_USER,
    CONF_BBUDDY_PASSWORD,
    CONF_DISCOVER_INTERVAL,
    CONF_UPLOAD_IMAGES,
    CONF_USE_GRAPHQL,
    CONF_GEMINI_API_KEY,
    CONF_GEMINI_MODEL,
    DEFAULT_USE_GRAPHQL,
    DEFAULT_DISCOVER_INTERVAL,
    DEFAULT_UPLOAD_IMAGES,
    DEFAULT_GEMINI_MODEL,
)

_LOGGER = logging.getLogger(__name__)

# Repo root is three levels up: custom_components/grocy_scraper/ -> repo root
_REPO_ROOT = Path(__file__).parent.parent.parent

# Logger namespaces whose records are captured for terminal output
_CAPTURE_NAMESPACES = ("grocy_scraper", "main")


def _ensure_repo_on_path() -> None:
    """Add the repository root to sys.path so the grocy_scraper package is importable."""
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


# ---------------------------------------------------------------------------
# Log-capturing helpers
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    """Logging handler that stores records emitted by grocy_scraper loggers."""

    _FORMATTER = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(self._FORMATTER)
        self.records: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "level": record.levelname,
                "message": self.format(record),
            }
        )


@contextmanager
def _capture_logs() -> Generator[list[dict[str, str]], None, None]:
    """Attach a capturing handler to each grocy_scraper logger for the duration.

    Yields the live list of captured {level, message} dicts so callers can
    read it after the ``with`` block exits.
    """
    handler = _CapturingHandler()
    target_loggers = [logging.getLogger(ns) for ns in _CAPTURE_NAMESPACES]
    for lgr in target_loggers:
        lgr.addHandler(handler)
        if lgr.level == logging.NOTSET or lgr.level > logging.DEBUG:
            lgr.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        for lgr in target_loggers:
            lgr.removeHandler(handler)


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register WebSocket commands exposed by this integration."""
    websocket_api.async_register_command(hass, ws_search_products)
    websocket_api.async_register_command(hass, ws_get_config)
    websocket_api.async_register_command(hass, ws_run_discover)
    websocket_api.async_register_command(hass, ws_run_sort)
    websocket_api.async_register_command(hass, ws_run_date)


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
    """Handle a product-search request from the sidebar panel."""
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
    except Exception as exc:  # noqa: BLE001
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
# grocy_scraper/get_config
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
    connection.send_result(
        msg["id"],
        {
            "configured": True,
            "store_id": entry.data.get(CONF_STORE_ID, ""),
            "discover_interval": entry.options.get(
                CONF_DISCOVER_INTERVAL, DEFAULT_DISCOVER_INTERVAL
            ),
            "bbuddy_configured": bool(
                entry.options.get(CONF_BBUDDY_URL)
                and entry.options.get(CONF_BBUDDY_USER)
                and entry.options.get(CONF_BBUDDY_PASSWORD)
            ),
            "gemini_configured": bool(entry.options.get(CONF_GEMINI_API_KEY)),
        },
    )


# ---------------------------------------------------------------------------
# grocy_scraper/run_discover
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "grocy_scraper/run_discover",
    }
)
@websocket_api.async_response
async def ws_run_discover(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Trigger an immediate Barcode Buddy -> K-Ruoka -> Grocy discover run."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        connection.send_error(
            msg["id"], "not_configured", "Grocy Scraper is not configured."
        )
        return

    entry = entries[0]
    try:
        result = await hass.async_add_executor_job(
            _run_discover_sync,
            dict(entry.data),
            dict(entry.options),
        )
        connection.send_result(msg["id"], result)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("run_discover failed")
        connection.send_error(msg["id"], "discover_failed", str(exc))


def _run_discover_sync(config: dict, options: dict) -> dict[str, Any]:
    """Execute the Barcode Buddy -> K-Ruoka -> Grocy discovery pipeline."""
    bbuddy_url = options.get(CONF_BBUDDY_URL, "")
    bbuddy_user = options.get(CONF_BBUDDY_USER, "")
    bbuddy_password = options.get(CONF_BBUDDY_PASSWORD, "")

    if not (bbuddy_url and bbuddy_user and bbuddy_password):
        return {
            "success": False,
            "skipped": True,
            "logs": [
                {
                    "level": "WARNING",
                    "message": "Barcode Buddy URL, username, and password are required "
                    "for Discover. Configure them in the integration options.",
                }
            ],
        }

    _ensure_repo_on_path()

    args = argparse.Namespace(
        store=config.get(CONF_STORE_ID, ""),
        grocy_url=config.get(CONF_GROCY_URL, ""),
        grocy_key=config.get(CONF_GROCY_KEY, ""),
        location_id=config.get(CONF_LOCATION_ID),
        quantity_unit_id=config.get(CONF_QUANTITY_UNIT_ID),
        bbuddy_url=bbuddy_url,
        bbuddy_key=options.get(CONF_BBUDDY_KEY, ""),
        bbuddy_user=bbuddy_user,
        bbuddy_password=bbuddy_password,
        upload_images=options.get(CONF_UPLOAD_IMAGES, DEFAULT_UPLOAD_IMAGES),
        use_graphql=options.get(CONF_USE_GRAPHQL, DEFAULT_USE_GRAPHQL),
    )

    import main as _main  # noqa: PLC0415

    with _capture_logs() as logs:
        result_code: int = _main._discover_products(args)

    return {"success": result_code == 0, "skipped": False, "logs": logs}


# ---------------------------------------------------------------------------
# grocy_scraper/run_sort
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "grocy_scraper/run_sort",
    }
)
@websocket_api.async_response
async def ws_run_sort(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Use Gemini AI to assign each Grocy product to an appropriate location."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        connection.send_error(
            msg["id"], "not_configured", "Grocy Scraper is not configured."
        )
        return

    entry = entries[0]
    try:
        result = await hass.async_add_executor_job(
            _run_sort_sync,
            dict(entry.data),
            dict(entry.options),
        )
        connection.send_result(msg["id"], result)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("run_sort failed")
        connection.send_error(msg["id"], "sort_failed", str(exc))


def _run_sort_sync(config: dict, options: dict) -> dict[str, Any]:
    """Run AI location-sorting of all Grocy products."""
    gemini_api_key = options.get(CONF_GEMINI_API_KEY, "")
    if not gemini_api_key:
        return {
            "success": False,
            "skipped": True,
            "updated": 0,
            "logs": [
                {
                    "level": "WARNING",
                    "message": "A Gemini API key is required for Sort. "
                    "Add it in the integration options.",
                }
            ],
        }

    _ensure_repo_on_path()

    from grocy_scraper.grocy_client import GrocyClient  # noqa: PLC0415
    import main as _main  # noqa: PLC0415

    grocy = GrocyClient(
        base_url=config.get(CONF_GROCY_URL, ""),
        api_key=config.get(CONF_GROCY_KEY, ""),
    )
    model = options.get(CONF_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)

    with _capture_logs() as logs:
        updated: int = _main._ai_sort_products(grocy, gemini_api_key, model)

    return {"success": True, "skipped": False, "updated": updated, "logs": logs}


# ---------------------------------------------------------------------------
# grocy_scraper/run_date
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "grocy_scraper/run_date",
    }
)
@websocket_api.async_response
async def ws_run_date(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Use Gemini AI to guess default best-before days for each Grocy product."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        connection.send_error(
            msg["id"], "not_configured", "Grocy Scraper is not configured."
        )
        return

    entry = entries[0]
    try:
        result = await hass.async_add_executor_job(
            _run_date_sync,
            dict(entry.data),
            dict(entry.options),
        )
        connection.send_result(msg["id"], result)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("run_date failed")
        connection.send_error(msg["id"], "date_failed", str(exc))


def _run_date_sync(config: dict, options: dict) -> dict[str, Any]:
    """Run AI best-before-date assignment for all Grocy products."""
    gemini_api_key = options.get(CONF_GEMINI_API_KEY, "")
    if not gemini_api_key:
        return {
            "success": False,
            "skipped": True,
            "updated": 0,
            "logs": [
                {
                    "level": "WARNING",
                    "message": "A Gemini API key is required for Date. "
                    "Add it in the integration options.",
                }
            ],
        }

    _ensure_repo_on_path()

    from grocy_scraper.grocy_client import GrocyClient  # noqa: PLC0415
    import main as _main  # noqa: PLC0415

    grocy = GrocyClient(
        base_url=config.get(CONF_GROCY_URL, ""),
        api_key=config.get(CONF_GROCY_KEY, ""),
    )
    model = options.get(CONF_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)

    with _capture_logs() as logs:
        updated: int = _main._ai_assign_due_dates(grocy, gemini_api_key, model)

    return {"success": True, "skipped": False, "updated": updated, "logs": logs}
