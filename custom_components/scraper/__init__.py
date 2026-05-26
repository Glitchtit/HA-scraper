"""Home Assistant integration for Scraper.

Provides a config flow and HA services for searching Finnish grocery products
(k-ruoka.fi / s-kaupat.fi) and adding them to Storage.

The Scraper sidebar UI is served by the **add-on's** ingress panel. This
integration deliberately does NOT register its own panel — doing so produced a
second, duplicate "Scraper" entry in the sidebar whenever both the add-on and
this integration were installed.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_URL
from . import services

_LOGGER = logging.getLogger(__name__)

# Key used to store state in hass.data
_KEY_SERVICES_REGISTERED = "services_registered"


# ---------------------------------------------------------------------------
# Component-level setup (runs once regardless of how many config entries)
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


# ---------------------------------------------------------------------------
# Config-entry setup / teardown
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scraper from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Clear any leftover panel from versions that registered one (see below).
    _remove_legacy_panel(hass)

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


def _remove_legacy_panel(hass: HomeAssistant) -> None:
    """Remove the duplicate "scraper" sidebar panel registered by older versions.

    The add-on's ingress panel is the canonical Scraper UI. Best-effort so a
    HACS upgrade clears the stale entry without waiting for a full HA restart;
    never blocks setup.
    """
    try:
        from homeassistant.components import frontend  # noqa: PLC0415

        if PANEL_URL in hass.data.get(frontend.DATA_PANELS, {}):
            frontend.async_remove_panel(hass, PANEL_URL)
            _LOGGER.info("Removed legacy duplicate 'scraper' sidebar panel.")
    except Exception:  # pragma: no cover - cleanup must never block setup
        _LOGGER.debug("Legacy scraper panel cleanup skipped", exc_info=True)
