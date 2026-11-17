"""Shared pytest fixtures for scraper tests."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

# The tests run the code that ships in the add-on image, where addon/ is the
# application root (/app): `main`, `ingress_server` and the `scraper` package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "addon"))

import ingress_server as ingress_mod  # noqa: E402
import main as main_mod  # noqa: E402
import scraper.scraper as scraper_mod  # noqa: E402
from scraper.searxng_client import SearXNGError  # noqa: E402
from scraper.skaupat_client import SKaupatError  # noqa: E402

from .fakes import FakeKRuoka, FakeStorage  # noqa: E402


@pytest.fixture(autouse=True)
def _state_files_in_tmp(tmp_path, monkeypatch):
    """The add-on keeps its state files in /data; tests keep them in tmp_path."""
    monkeypatch.setattr(
        main_mod, "_price_state_path", lambda: tmp_path / "price_refresh.json"
    )
    monkeypatch.setattr(
        scraper_mod, "_cf_cache_path", lambda: tmp_path / "cf_clearance.json"
    )
    monkeypatch.setattr(
        ingress_mod, "_nightly_state_path", lambda: tmp_path / "nightly_update.json"
    )


@pytest.fixture(autouse=True)
def _fresh_registered_stores():
    """Stores are named in Storage once per process; start each test clean."""
    with patch.object(main_mod, "_registered_stores", set()):
        yield


@pytest.fixture(autouse=True)
def _fresh_krapi_throttle():
    """Give each test a fresh zero-interval kr-api throttle.

    The production throttle is module-global (shared pacing across scraper
    instances); without a reset, one test's 429 penalty would make the next
    test really sleep.
    """
    with patch.object(
        scraper_mod, "_krapi_throttle", scraper_mod._KrApiThrottle(min_interval=0)
    ), patch.object(
        scraper_mod, "_krapi_circuit", scraper_mod._KrApiCircuit()
    ), patch.object(scraper_mod, "_cf_solution", None):
        yield


@pytest.fixture
def world(monkeypatch):
    """The scraper's outside world, faked: Storage, K-Ruoka (both APIs),
    S-kaupat, SearXNG and image downloads.

    ``world.storage`` and ``world.kruoka`` are the fakes. ``world.skaupat``
    and ``world.searxng`` map an EAN to the product that source finds;
    adding "skaupat" or "searxng" to ``world.down`` makes that source fail
    (``world.kruoka.down`` does the same for K-Ruoka). ``world.asked`` lists
    the fallback lookups made, ``world.downloads`` the image URLs fetched.
    """
    w = SimpleNamespace(
        storage=FakeStorage(), kruoka=FakeKRuoka(),
        skaupat={}, searxng={}, down=set(), asked=[], downloads=[],
    )

    def skaupat(ean, **kwargs):
        w.asked.append(("skaupat", ean))
        if "skaupat" in w.down:
            raise SKaupatError("S-kaupat unreachable")
        return w.skaupat.get(ean)

    def searxng(ean, **kwargs):
        w.asked.append(("searxng", ean))
        if "searxng" in w.down:
            raise SearXNGError("SearXNG unreachable")
        return w.searxng.get(ean)

    def download(url, timeout=None):
        w.downloads.append(url)
        resp = MagicMock()
        resp.content = b"image"
        resp.headers = {"Content-Type": "image/jpeg"}
        resp.raise_for_status.return_value = None
        return resp

    def storage(base_url, **kwargs):
        return w.storage

    monkeypatch.setattr(main_mod, "StorageClient", storage)
    monkeypatch.setattr("scraper.storage_client.StorageClient", storage)
    monkeypatch.setattr(scraper_mod, "_make_graphql_session", lambda: w.kruoka)
    monkeypatch.setattr(scraper_mod, "_make_krapi_session", lambda: w.kruoka)
    monkeypatch.setattr("scraper.lookup.skaupat_lookup", skaupat)
    monkeypatch.setattr("scraper.lookup.searxng_lookup", searxng)
    monkeypatch.setattr(requests, "get", download)
    # The code under test exports this option to the environment itself.
    monkeypatch.delenv("FLARESOLVERR_URL", raising=False)
    return w
