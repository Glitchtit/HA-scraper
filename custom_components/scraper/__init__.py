"""Home Assistant integration for Scraper.

This integration exposes a sidebar panel that lets users search for Finnish
grocery products on k-ruoka.fi and add them to Storage.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    CONF_STORAGE_URL,
    CONF_STORE_ID,
    CONF_UPLOAD_IMAGES,
    CONF_USE_GRAPHQL,
    DEFAULT_UPLOAD_IMAGES,
    DEFAULT_USE_GRAPHQL,
)
from . import ws_api
from . import services

_LOGGER = logging.getLogger(__name__)

# Repository root lives three levels above this file:
#   custom_components/scraper/__init__.py  →  repo root
_REPO_ROOT = Path(__file__).parent.parent.parent

# Key used to store state in hass.data
_KEY_WS_REGISTERED = "ws_registered"
_KEY_PANEL_REGISTERED = "panel_registered"
_KEY_STATIC_REGISTERED = "static_registered"
_KEY_SERVICES_REGISTERED = "services_registered"


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
            "/scraper_panel",
            str(Path(__file__).parent / "www"),
            cache_headers=False,
        )
        hass.data[DOMAIN][_KEY_STATIC_REGISTERED] = True

    return True


# ---------------------------------------------------------------------------
# Config-entry setup / teardown
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scraper from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Register the sidebar panel (idempotent guard so reloads don't fail).
    if not hass.data[DOMAIN].get(_KEY_PANEL_REGISTERED):
        await _async_register_panel(hass)
        hass.data[DOMAIN][_KEY_PANEL_REGISTERED] = True

    # Register WebSocket commands (idempotent guard).
    if not hass.data[DOMAIN].get(_KEY_WS_REGISTERED):
        ws_api.async_register(hass)
        hass.data[DOMAIN][_KEY_WS_REGISTERED] = True

    # Register HA services (idempotent guard).
    if not hass.data[DOMAIN].get(_KEY_SERVICES_REGISTERED):
        services.async_register_services(hass)
        hass.data[DOMAIN][_KEY_SERVICES_REGISTERED] = True

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Tear down services only when no other scraper entries remain.
    remaining = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id
    ]
    if not remaining:
        services.async_unregister_services(hass)
        hass.data.get(DOMAIN, {}).pop(_KEY_SERVICES_REGISTERED, None)
    return True


# ---------------------------------------------------------------------------
# Sidebar panel registration
# ---------------------------------------------------------------------------


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register the custom sidebar panel."""
    from homeassistant.components.panel_custom import async_register_panel  # noqa: PLC0415

    await async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name="scraper-panel",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        js_url="/scraper_panel/panel.js",
        embed_iframe=False,
        require_admin=False,
    )
