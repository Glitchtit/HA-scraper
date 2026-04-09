"""Home Assistant integration for Grocy Scraper.

This integration exposes a sidebar panel that lets users search for Finnish
grocery products on k-ruoka.fi and add them to a Grocy inventory database.
It also supports automatic product discovery via the Storage barcode queue
on a configurable time interval.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    CONF_GROCY_URL,
    CONF_GROCY_KEY,
    CONF_STORE_ID,
    CONF_LOCATION_ID,
    CONF_QUANTITY_UNIT_ID,
    CONF_DISCOVER_INTERVAL,
    CONF_UPLOAD_IMAGES,
    CONF_USE_GRAPHQL,
    DEFAULT_DISCOVER_INTERVAL,
    DEFAULT_UPLOAD_IMAGES,
    DEFAULT_USE_GRAPHQL,
)
from . import ws_api

_LOGGER = logging.getLogger(__name__)

# Repository root lives three levels above this file:
#   custom_components/grocy_scraper/__init__.py  →  repo root
_REPO_ROOT = Path(__file__).parent.parent.parent

# Key used to store the interval-tracker cancel callback in hass.data
_KEY_CANCEL_DISCOVER = "cancel_discover"
_KEY_WS_REGISTERED = "ws_registered"
_KEY_PANEL_REGISTERED = "panel_registered"
_KEY_STATIC_REGISTERED = "static_registered"


def _ensure_repo_on_path() -> None:
    """Prepend the repository root to sys.path once."""
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


# ---------------------------------------------------------------------------
# Component-level setup (runs once regardless of how many config entries)
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register static assets served to the frontend."""
    hass.data.setdefault(DOMAIN, {})

    if not hass.data[DOMAIN].get(_KEY_STATIC_REGISTERED):
        hass.http.register_static_path(
            "/grocy_scraper_panel",
            str(Path(__file__).parent / "www"),
            cache_headers=False,
        )
        hass.data[DOMAIN][_KEY_STATIC_REGISTERED] = True

    return True


# ---------------------------------------------------------------------------
# Config-entry setup / teardown
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Grocy Scraper from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Register the sidebar panel (idempotent guard so reloads don't fail).
    if not hass.data[DOMAIN].get(_KEY_PANEL_REGISTERED):
        await _async_register_panel(hass)
        hass.data[DOMAIN][_KEY_PANEL_REGISTERED] = True

    # Register WebSocket commands (idempotent guard).
    if not hass.data[DOMAIN].get(_KEY_WS_REGISTERED):
        ws_api.async_register(hass)
        hass.data[DOMAIN][_KEY_WS_REGISTERED] = True

    # Schedule periodic discover runs.
    _schedule_discover(hass, entry)

    # Re-schedule when the user changes options.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and cancel any pending discover tasks."""
    _cancel_discover(hass, entry)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-schedule discover when the options (interval) change."""
    _cancel_discover(hass, entry)
    _schedule_discover(hass, entry)


# ---------------------------------------------------------------------------
# Sidebar panel registration
# ---------------------------------------------------------------------------


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register the custom sidebar panel."""
    from homeassistant.components.panel_custom import async_register_panel  # noqa: PLC0415

    await async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name="grocy-scraper-panel",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        js_url="/grocy_scraper_panel/panel.js",
        embed_iframe=False,
        require_admin=False,
    )


# ---------------------------------------------------------------------------
# Periodic discover scheduler
# ---------------------------------------------------------------------------


def _schedule_discover(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up a recurring timer that calls _discover_products_async."""
    interval_minutes: int = entry.options.get(
        CONF_DISCOVER_INTERVAL, DEFAULT_DISCOVER_INTERVAL
    )

    async def _periodic_discover(_now: Any) -> None:
        _LOGGER.debug("Running scheduled --discover …")
        await hass.async_add_executor_job(_run_discover_sync, entry.data, entry.options)

    cancel = async_track_time_interval(
        hass,
        _periodic_discover,
        timedelta(minutes=interval_minutes),
    )

    # Store the cancel handle so we can remove it on reload / unload.
    hass.data[DOMAIN].setdefault("entries", {})[entry.entry_id] = {
        _KEY_CANCEL_DISCOVER: cancel,
    }
    entry.async_on_unload(cancel)

    _LOGGER.debug(
        "Discover scheduled every %d minute(s) for entry %s.",
        interval_minutes,
        entry.entry_id,
    )


def _cancel_discover(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cancel any existing periodic discover timer for *entry*."""
    entry_data = hass.data.get(DOMAIN, {}).get("entries", {}).pop(entry.entry_id, {})
    cancel = entry_data.get(_KEY_CANCEL_DISCOVER)
    if cancel is not None:
        cancel()


# ---------------------------------------------------------------------------
# Discover runner (synchronous, called from executor)
# ---------------------------------------------------------------------------


def _run_discover_sync(config: dict, options: dict) -> dict:
    """Execute the barcode queue → K-Ruoka → Grocy discovery pipeline.

    Returns a dict with ``success`` (bool) and ``result_code`` (int).
    """
    _ensure_repo_on_path()

    # Build an argparse.Namespace that matches what main._discover_products expects.
    args = argparse.Namespace(
        store=config.get(CONF_STORE_ID, ""),
        storage_url=config.get(CONF_GROCY_URL, ""),
        location_id=config.get(CONF_LOCATION_ID),
        quantity_unit_id=config.get(CONF_QUANTITY_UNIT_ID),
        upload_images=options.get(CONF_UPLOAD_IMAGES, DEFAULT_UPLOAD_IMAGES),
        use_graphql=options.get(CONF_USE_GRAPHQL, DEFAULT_USE_GRAPHQL),
    )

    from grocy_scraper_addon import main as _main  # noqa: PLC0415

    result_code: int = _main._discover_products(args)
    return {"success": result_code == 0, "result_code": result_code}
