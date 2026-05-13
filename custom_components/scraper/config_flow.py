"""Config flow for the Scraper integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_STORAGE_URL,
    CONF_STORE_ID,
    CONF_UPLOAD_IMAGES,
    CONF_USE_GRAPHQL,
    CONF_GEMINI_API_KEY,
    CONF_GEMINI_MODEL,
    CONF_GEMINI_MODEL_OPTIMIZE,
    DEFAULT_UPLOAD_IMAGES,
    DEFAULT_USE_GRAPHQL,
    DEFAULT_GEMINI_MODEL,
)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STORAGE_URL): str,
        vol.Required(CONF_STORE_ID): str,
    }
)


class ScraperConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration flow for Scraper."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        """Show the setup form and create the config entry on submission."""
        errors: dict[str, str] = {}

        if user_input is not None:
            for field in (CONF_STORAGE_URL, CONF_STORE_ID):
                if not str(user_input.get(field, "")).strip():
                    errors[field] = "required"

            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_STORAGE_URL]}_{user_input[CONF_STORE_ID]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Scraper ({user_input[CONF_STORE_ID]})",
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
    ) -> "ScraperOptionsFlow":
        """Return the options flow handler."""
        return ScraperOptionsFlow(config_entry)


class ScraperOptionsFlow(config_entries.OptionsFlow):
    """Handle the options flow (AI settings)."""

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
