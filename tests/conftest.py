"""Shared pytest fixtures for grocy_scraper tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _skip_storage_wait():
    """Prevent wait_for_storage() from blocking during tests."""
    with patch("grocy_scraper_addon.main.wait_for_storage", return_value=None):
        yield
