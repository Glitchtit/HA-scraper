"""Config flow for the Grocy Scraper integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

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
    CONF_GEMINI_MODEL_OPTIMIZE,
    DEFAULT_DISCOVER_INTERVAL,
    DEFAULT_UPLOAD_IMAGES,
    DEFAULT_USE_GRAPHQL,
    DEFAULT_GEMINI_MODEL,
)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GROCY_URL): str,
        vol.Required(CONF_GROCY_KEY): str,
        vol.Required(CONF_STORE_ID): str,
        vol.Required(CONF_LOCATION_ID): vol.Coerce(int),
        vol.Required(CONF_QUANTITY_UNIT_ID): vol.Coerce(int),
    }
)


class GrocyScraperConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration flow for Grocy Scraper."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        """Show the setup form and create the config entry on submission."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Basic validation
            for field in (CONF_GROCY_URL, CONF_GROCY_KEY, CONF_STORE_ID):
                if not str(user_input.get(field, "")).strip():
                    errors[field] = "required"

            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_GROCY_URL]}_{user_input[CONF_STORE_ID]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Grocy Scraper ({user_input[CONF_STORE_ID]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "GrocyScraperOptionsFlow":
        """Return the options flow handler."""
        return GrocyScraperOptionsFlow(config_entry)


class GrocyScraperOptionsFlow(config_entries.OptionsFlow):
    """Handle the options flow (Barcode Buddy credentials + discover interval)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise with current options."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        """Display and handle the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_BBUDDY_URL,
                    default=opts.get(CONF_BBUDDY_URL, ""),
                ): str,
                vol.Optional(
                    CONF_BBUDDY_KEY,
                    default=opts.get(CONF_BBUDDY_KEY, ""),
                ): str,
                vol.Optional(
                    CONF_BBUDDY_USER,
                    default=opts.get(CONF_BBUDDY_USER, ""),
                ): str,
                vol.Optional(
                    CONF_BBUDDY_PASSWORD,
                    default=opts.get(CONF_BBUDDY_PASSWORD, ""),
                ): str,
                vol.Optional(
                    CONF_DISCOVER_INTERVAL,
                    default=opts.get(CONF_DISCOVER_INTERVAL, DEFAULT_DISCOVER_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional(
                    CONF_UPLOAD_IMAGES,
                    default=opts.get(CONF_UPLOAD_IMAGES, DEFAULT_UPLOAD_IMAGES),
                ): bool,
                vol.Optional(
                    CONF_USE_GRAPHQL,
                    default=opts.get(CONF_USE_GRAPHQL, DEFAULT_USE_GRAPHQL),
                ): bool,
                vol.Optional(
                    CONF_GEMINI_API_KEY,
                    default=opts.get(CONF_GEMINI_API_KEY, ""),
                ): str,
                vol.Optional(
                    CONF_GEMINI_MODEL,
                    default=opts.get(CONF_GEMINI_MODEL, DEFAULT_GEMINI_MODEL),
                ): str,
                vol.Optional(
                    CONF_GEMINI_MODEL_OPTIMIZE,
                    default=opts.get(CONF_GEMINI_MODEL_OPTIMIZE, ""),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
