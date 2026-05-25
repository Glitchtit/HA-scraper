"""Home Assistant service handlers for the Scraper integration.

Exposes two agent-callable services:

* ``scraper.search_products`` (SupportsResponse.ONLY) — wraps the same
  K-Ruoka product search used by the ``scraper/search`` WebSocket command.
* ``scraper.add_product`` (SupportsResponse.OPTIONAL) — creates a found
  product in HA-Storage (product → optional barcode → optional image).

The integration imports the top-level ``scraper`` package by prepending the
repo root to ``sys.path`` (mirroring ``ws_api._ensure_repo_on_path``). All
blocking work runs inside ``hass.async_add_executor_job``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import config_validation as cv
    _HA_AVAILABLE = True
except ImportError:  # running in bare test env without homeassistant installed
    _HA_AVAILABLE = False
    HomeAssistant = object  # type: ignore[assignment,misc]
    ServiceCall = object  # type: ignore[assignment,misc]
    ConfigEntry = object  # type: ignore[assignment,misc]

    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        pass

    class SupportsResponse:  # type: ignore[no-redef]
        ONLY = "only"
        OPTIONAL = "optional"

    class cv:  # type: ignore[no-redef]
        @staticmethod
        def string(value):
            return str(value)

from .const import (
    DOMAIN,
    CONF_STORAGE_URL,
    CONF_STORE_ID,
    CONF_USE_GRAPHQL,
    DEFAULT_USE_GRAPHQL,
)

_LOGGER = logging.getLogger(__name__)

# Repo root is three levels up: custom_components/scraper/ -> repo root
_REPO_ROOT = Path(__file__).parent.parent.parent

SERVICE_SEARCH_PRODUCTS = "search_products"
SERVICE_ADD_PRODUCT = "add_product"


def _ensure_repo_on_path() -> None:
    """Add the repository root to sys.path so the scraper package is importable."""
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def shape_search_results(raw: list[Any]) -> list[dict[str, str]]:
    """Map raw search rows to the stable ``{name, ean, description, image_url}`` shape.

    Accepts either ``scraper.scraper.Product`` dataclass instances or plain
    dicts (as produced by ``ws_api._search_products_sync``). Missing optional
    fields default to an empty string. Extra fields are dropped.
    """
    shaped: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name", "")
            ean = item.get("ean", "")
            description = item.get("description", "")
            image_url = item.get("image_url", "")
        else:
            name = getattr(item, "name", "")
            ean = getattr(item, "ean", "")
            description = getattr(item, "description", "")
            image_url = getattr(item, "image_url", "")
        shaped.append(
            {
                "name": name or "",
                "ean": ean or "",
                "description": description or "",
                "image_url": image_url or "",
            }
        )
    return shaped
