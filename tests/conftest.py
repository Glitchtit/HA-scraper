"""Shared pytest fixtures for scraper tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _skip_storage_wait():
    """Prevent wait_for_storage() from blocking during tests."""
    with patch("addon.main.wait_for_storage", return_value=None):
        yield
