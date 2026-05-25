"""Unit tests for the scraper integration service layer.

These tests import the integration module by file path (it lives under
custom_components/, which is not importable as a normal package from the
test root) and exercise the pure helper plus the add_product orchestration
with a mocked StorageClient. No Home Assistant runtime or network is used.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from scraper.scraper import Product
from scraper.storage_client import StorageAPIError, StorageClient

# ---------------------------------------------------------------------------
# Bootstrap: inject a fake "scraper_integration" package so that relative
# imports inside services.py (e.g. ``from .const import DOMAIN``) resolve
# without needing the full custom_components tree on sys.path.
# ---------------------------------------------------------------------------

_CC_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "scraper"

# Fake parent package
_PKG_NAME = "scraper_integration"
_pkg = ModuleType(_PKG_NAME)
_pkg.__path__ = [str(_CC_ROOT)]  # type: ignore[attr-defined]
_pkg.__package__ = _PKG_NAME
sys.modules[_PKG_NAME] = _pkg

# Load const.py as scraper_integration.const
_const_spec = importlib.util.spec_from_file_location(
    f"{_PKG_NAME}.const", _CC_ROOT / "const.py"
)
_const_mod = importlib.util.module_from_spec(_const_spec)
_const_mod.__package__ = _PKG_NAME
sys.modules[f"{_PKG_NAME}.const"] = _const_mod
_const_spec.loader.exec_module(_const_mod)

# Load custom_components/scraper/services.py directly by path so the test
# does not depend on Home Assistant package layout.
_SERVICES_PATH = _CC_ROOT / "services.py"
_spec = importlib.util.spec_from_file_location(
    f"{_PKG_NAME}.services",
    _SERVICES_PATH,
    submodule_search_locations=[],
)
_spec.submodule_search_locations = None  # mark as a module, not a package
services = importlib.util.module_from_spec(_spec)
sys.modules[f"{_PKG_NAME}.services"] = services
_spec.loader.exec_module(services)


class TestShapeSearchResults:
    def test_shapes_product_dataclasses(self):
        raw = [
            Product(
                name="Sprite 0,33 l",
                ean="1234567890123",
                product_id="abc",
                description="Virvoitusjuoma",
                image_url="https://img/sprite.webp",
                extra={"price": 1.5},
            )
        ]
        assert services.shape_search_results(raw) == [
            {
                "name": "Sprite 0,33 l",
                "ean": "1234567890123",
                "description": "Virvoitusjuoma",
                "image_url": "https://img/sprite.webp",
            }
        ]

    def test_shapes_plain_dicts(self):
        raw = [
            {
                "name": "Maito 1 l",
                "ean": "2000000000001",
                "description": "",
                "image_url": "",
                "extra_field": "ignored",
            }
        ]
        assert services.shape_search_results(raw) == [
            {
                "name": "Maito 1 l",
                "ean": "2000000000001",
                "description": "",
                "image_url": "",
            }
        ]

    def test_missing_optional_fields_default_to_empty_string(self):
        raw = [Product(name="Pelkkä nimi", ean="")]
        assert services.shape_search_results(raw) == [
            {"name": "Pelkkä nimi", "ean": "", "description": "", "image_url": ""}
        ]

    def test_empty_input_returns_empty_list(self):
        assert services.shape_search_results([]) == []


class TestAddProductSync:
    def _client(self) -> MagicMock:
        storage = MagicMock(spec=StorageClient)
        storage.create_product.return_value = 77
        return storage

    def test_name_only_creates_product_no_barcode_no_image(self):
        storage = self._client()
        fetch_image = MagicMock()  # must NOT be called when no image_url
        pid = services.add_product_sync(
            storage,
            name="Uusi tuote",
            ean="",
            description="",
            image_url="",
            fetch_image=fetch_image,
        )
        assert pid == 77
        storage.create_product.assert_called_once_with(
            name="Uusi tuote", description=""
        )
        storage.add_barcode.assert_not_called()
        storage.upload_product_image.assert_not_called()
        fetch_image.assert_not_called()

    def test_with_ean_adds_barcode(self):
        storage = self._client()
        fetch_image = MagicMock()
        pid = services.add_product_sync(
            storage,
            name="Sprite 0,33 l",
            ean="1234567890123",
            description="Virvoitusjuoma",
            image_url="",
            fetch_image=fetch_image,
        )
        assert pid == 77
        storage.create_product.assert_called_once_with(
            name="Sprite 0,33 l", description="Virvoitusjuoma"
        )
        storage.add_barcode.assert_called_once_with(77, barcode="1234567890123")
        storage.upload_product_image.assert_not_called()
        fetch_image.assert_not_called()

    def test_with_image_downloads_and_uploads(self):
        storage = self._client()
        fetch_image = MagicMock(
            return_value=("sprite.webp", b"\x89PNGbytes", "image/webp")
        )
        pid = services.add_product_sync(
            storage,
            name="Sprite",
            ean="1234567890123",
            description="",
            image_url="https://img/sprite.webp",
            fetch_image=fetch_image,
        )
        assert pid == 77
        storage.create_product.assert_called_once_with(name="Sprite", description="")
        storage.add_barcode.assert_called_once_with(77, barcode="1234567890123")
        fetch_image.assert_called_once_with("https://img/sprite.webp")
        storage.upload_product_image.assert_called_once_with(
            77, "sprite.webp", b"\x89PNGbytes", content_type="image/webp"
        )

    def test_image_skipped_when_download_returns_none(self):
        # fetch_image returning None means the image could not be retrieved;
        # product creation must still succeed.
        storage = self._client()
        fetch_image = MagicMock(return_value=None)
        pid = services.add_product_sync(
            storage,
            name="Sprite",
            ean="",
            description="",
            image_url="https://img/broken.webp",
            fetch_image=fetch_image,
        )
        assert pid == 77
        fetch_image.assert_called_once_with("https://img/broken.webp")
        storage.upload_product_image.assert_not_called()

    def test_create_product_failure_propagates(self):
        storage = self._client()
        storage.create_product.side_effect = StorageAPIError("boom")
        with pytest.raises(StorageAPIError):
            services.add_product_sync(
                storage,
                name="X",
                ean="",
                description="",
                image_url="",
                fetch_image=MagicMock(),
            )
