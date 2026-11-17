"""Home Assistant service handlers for the Scraper integration.

Exposes two agent-callable services:

* ``scraper.search_products`` (SupportsResponse.ONLY) — wraps the same
  K-Ruoka product search used by the add-on's ``/api/search``.
* ``scraper.add_product`` (SupportsResponse.OPTIONAL) — creates a found
  product in HA-Storage with the library's ``create_in_storage``, the same
  sequence the add-on uses.

The integration uses the vendored ``scraperlib`` subpackage bundled inside
``custom_components/scraper/scraperlib/`` so it works on a clean HACS install
without any sibling ``scraper/`` package on sys.path.
All blocking work runs inside ``hass.async_add_executor_job``.
"""

from __future__ import annotations

import logging
from typing import Any

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

from .const import DOMAIN, CONF_STORAGE_URL, CONF_STORE_ID

_LOGGER = logging.getLogger(__name__)

SERVICE_SEARCH_PRODUCTS = "search_products"
SERVICE_ADD_PRODUCT = "add_product"


def shape_search_results(raw: list[Any]) -> list[dict[str, str]]:
    """Map raw search rows to the stable ``{name, ean, description, image_url}`` shape.

    Accepts either ``scraper.scraper.Product`` dataclass instances or plain
    dicts (as produced by the scraperlib search). Missing optional fields
    default to an empty string. Extra fields are dropped.
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


def _entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the single scraper config entry, raising if unconfigured."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("Scraper is not configured.")
    return entries[0]


def _search_sync(store_id: str, query: str, max_products: int):
    """Run a synchronous K-Ruoka search and return shaped result dicts."""
    from .scraperlib.scraper import KRuokaScraper  # noqa: PLC0415

    scraper = KRuokaScraper(store_id=store_id)
    products = list(scraper.search(query, max_products=max_products))
    return shape_search_results(products)


def _add_product_sync_worker(storage_url: str, data: dict[str, Any]) -> int:
    """Create the product in Storage the way the add-on does; return its id."""
    from .scraperlib.scraper import Product  # noqa: PLC0415
    from .scraperlib.storage_client import (  # noqa: PLC0415
        StorageAPIError,
        StorageClient,
        create_in_storage,
    )

    ean = data.get("ean", "") or ""
    product_id = create_in_storage(
        StorageClient(base_url=storage_url),
        Product(
            name=data["name"],
            ean=ean,
            description=data.get("description", "") or "",
            image_url=data.get("image_url", "") or "",
        ),
        ean=ean,
    )
    if product_id is None:
        raise StorageAPIError(f"Barcode {ean} already belongs to a product")
    return product_id


_SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required("query"): cv.string,
        vol.Optional("max_products", default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=500)
        ),
    }
)

_ADD_PRODUCT_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Optional("ean", default=""): cv.string,
        vol.Optional("description", default=""): cv.string,
        vol.Optional("image_url", default=""): cv.string,
    }
)


def async_register_services(hass: HomeAssistant) -> None:
    """Register the scraper services. Idempotent."""
    if not _HA_AVAILABLE:
        raise RuntimeError(
            "scraper.services: Home Assistant runtime not available; "
            "cannot register services (import stubs are test-only)."
        )
    if hass.services.has_service(DOMAIN, SERVICE_SEARCH_PRODUCTS):
        return

    async def handle_search(call: ServiceCall) -> dict[str, Any]:
        entry = _entry(hass)
        store_id: str = entry.data.get(CONF_STORE_ID, "")
        try:
            products = await hass.async_add_executor_job(
                _search_sync,
                store_id,
                call.data["query"],
                call.data["max_products"],
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("scraper.search_products failed")
            raise HomeAssistantError(f"Product search failed: {exc}") from exc
        return {"products": products}

    async def handle_add(call: ServiceCall) -> dict[str, Any]:
        entry = _entry(hass)
        storage_url: str = entry.data.get(CONF_STORAGE_URL, "")
        if not storage_url:
            raise HomeAssistantError("Scraper has no Storage URL configured.")
        try:
            product_id = await hass.async_add_executor_job(
                _add_product_sync_worker,
                storage_url,
                dict(call.data),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("scraper.add_product failed")
            raise HomeAssistantError(f"Could not add product: {exc}") from exc
        return {"product_id": product_id}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH_PRODUCTS,
        handle_search,
        schema=_SEARCH_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_PRODUCT,
        handle_add,
        schema=_ADD_PRODUCT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the scraper services."""
    for svc in (SERVICE_SEARCH_PRODUCTS, SERVICE_ADD_PRODUCT):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)
