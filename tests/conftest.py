"""Shared pytest fixtures for scraper tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _skip_storage_wait():
    """Prevent wait_for_storage() from blocking during tests."""
    with patch("addon.main.wait_for_storage", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _fresh_krapi_throttle():
    """Give each test a fresh zero-interval kr-api throttle.

    The production throttle is module-global (shared pacing across scraper
    instances); without a reset, one test's 429 penalty would make the next
    test really sleep.
    """
    import scraper.scraper as scraper_mod

    with patch.object(
        scraper_mod, "_krapi_throttle", scraper_mod._KrApiThrottle(min_interval=0)
    ), patch.object(scraper_mod, "_cf_solution", None):
        yield
