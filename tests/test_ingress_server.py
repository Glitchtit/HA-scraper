"""Tests for the add-on ingress web server (grocy_scraper_addon/ingress_server.py)."""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixture: import the ingress_server module from grocy_scraper_addon/
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingress_mod() -> ModuleType:
    """Import ingress_server.py as a module without running ``__main__``."""
    path = "grocy_scraper_addon/ingress_server.py"
    spec = importlib.util.spec_from_file_location("ingress_server", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _read_options
# ---------------------------------------------------------------------------


class TestReadOptions:
    def test_returns_empty_dict_when_file_missing(self, ingress_mod: ModuleType) -> None:
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            assert ingress_mod._read_options() == {}

    def test_returns_empty_dict_on_invalid_json(self, ingress_mod: ModuleType, tmp_path: Path) -> None:
        bad = tmp_path / "options.json"
        bad.write_text("not json")
        with mock.patch.object(ingress_mod, "_OPTIONS_PATH", str(bad)):
            assert ingress_mod._read_options() == {}

    def test_returns_parsed_options(self, ingress_mod: ModuleType, tmp_path: Path) -> None:
        opts_file = tmp_path / "options.json"
        opts_file.write_text(json.dumps({"grocy_url": "http://grocy", "store_id": "N110"}))
        with mock.patch.object(ingress_mod, "_OPTIONS_PATH", str(opts_file)):
            result = ingress_mod._read_options()
        assert result["grocy_url"] == "http://grocy"
        assert result["store_id"] == "N110"


# ---------------------------------------------------------------------------
# _build_args
# ---------------------------------------------------------------------------


class TestBuildArgs:
    def test_basic_fields(self, ingress_mod: ModuleType) -> None:
        opts = {
            "store_id": "N110",
            "grocy_url": "http://grocy:9283",
            "grocy_api_key": "key123",
            "location_id": 5,
            "quantity_unit_id": 3,
        }
        ns = ingress_mod._build_args(opts)
        assert ns.store == "N110"
        assert ns.grocy_url == "http://grocy:9283"
        assert ns.grocy_key == "key123"
        assert ns.location_id == 5
        assert ns.quantity_unit_id == 3

    def test_defaults(self, ingress_mod: ModuleType) -> None:
        ns = ingress_mod._build_args({})
        assert ns.store == ""
        assert ns.upload_images is True
        assert ns.use_graphql is True
        assert ns.verbose is False
        assert ns.dry_run is False
        assert ns.skip_existing is True
        assert ns.max_products is None

    def test_overrides(self, ingress_mod: ModuleType) -> None:
        ns = ingress_mod._build_args({"store_id": "X"}, store="OVERRIDE", verbose=True)
        assert ns.store == "OVERRIDE"
        assert ns.verbose is True


# ---------------------------------------------------------------------------
# _capture_logs
# ---------------------------------------------------------------------------


class TestCaptureLogs:
    def test_captures_info(self, ingress_mod: ModuleType) -> None:
        with ingress_mod._capture_logs() as logs:
            logging.getLogger("grocy_scraper").info("hello world")
        assert len(logs) >= 1
        assert logs[0]["level"] == "INFO"
        assert "hello world" in logs[0]["message"]

    def test_captures_debug(self, ingress_mod: ModuleType) -> None:
        with ingress_mod._capture_logs() as logs:
            logging.getLogger("main").debug("debug msg")
        assert any(r["level"] == "DEBUG" for r in logs)

    def test_handler_removed_after_context(self, ingress_mod: ModuleType) -> None:
        lgr = logging.getLogger("grocy_scraper")
        before = len(lgr.handlers)
        with ingress_mod._capture_logs():
            during = len(lgr.handlers)
        after = len(lgr.handlers)
        assert during == before + 1
        assert after == before


# ---------------------------------------------------------------------------
# _handle_config
# ---------------------------------------------------------------------------


class TestHandleConfig:
    def test_no_options_file(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            cfg = ingress_mod._handle_config()
        assert cfg["configured"] is False
        assert cfg["bbuddy_configured"] is False
        assert cfg["gemini_configured"] is False
        assert cfg["discover_interval"] == 60

    def test_fully_configured(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "store_id": "N110",
            "discover_interval": 30,
            "bbuddy_url": "http://bb",
            "bbuddy_user": "admin",
            "bbuddy_password": "secret",
            "gemini_api_key": "gem-key",
        }
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            cfg = ingress_mod._handle_config()
        assert cfg["configured"] is True
        assert cfg["store_id"] == "N110"
        assert cfg["discover_interval"] == 30
        assert cfg["bbuddy_configured"] is True
        assert cfg["gemini_configured"] is True


# ---------------------------------------------------------------------------
# _handle_search
# ---------------------------------------------------------------------------


class TestHandleSearch:
    def test_empty_query(self, ingress_mod: ModuleType) -> None:
        result = ingress_mod._handle_search({})
        assert result["success"] is False
        assert "required" in result["error"].lower()

    def test_blank_query(self, ingress_mod: ModuleType) -> None:
        result = ingress_mod._handle_search({"query": "   "})
        assert result["success"] is False

    def test_successful_search(self, ingress_mod: ModuleType) -> None:
        from grocy_scraper.scraper import Product

        fake_products = [
            Product(name="Maito 1L", ean="6411234000001", description="Kevytmaito"),
        ]
        mock_scraper = mock.MagicMock()
        mock_scraper.search.return_value = iter(fake_products)

        with mock.patch.object(ingress_mod, "_read_options", return_value={"store_id": "N110"}):
            with mock.patch("grocy_scraper.scraper.KRuokaScraper", return_value=mock_scraper):
                result = ingress_mod._handle_search({"query": "maito", "max_products": 10})

        assert result["success"] is True
        assert len(result["products"]) == 1
        assert result["products"][0]["name"] == "Maito 1L"
        assert result["products"][0]["ean"] == "6411234000001"

    def test_search_exception(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            with mock.patch(
                "grocy_scraper.scraper.KRuokaScraper",
                side_effect=RuntimeError("connection error"),
            ):
                result = ingress_mod._handle_search({"query": "test"})
        assert result["success"] is False
        assert "connection error" in result["error"]


# ---------------------------------------------------------------------------
# _handle_discover
# ---------------------------------------------------------------------------


class TestHandleDiscover:
    def test_missing_bbuddy_config(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            result = ingress_mod._handle_discover()
        assert result["success"] is False
        assert result["skipped"] is True
        assert len(result["logs"]) == 1
        assert result["logs"][0]["level"] == "WARNING"

    def test_calls_discover_products(self, ingress_mod: ModuleType) -> None:
        opts = {
            "bbuddy_url": "http://bb",
            "bbuddy_user": "admin",
            "bbuddy_password": "pass",
            "store_id": "N110",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
        }
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                sys.modules["main"]._discover_products.return_value = (0, [10])
                result = ingress_mod._handle_discover()

        assert result["success"] is True
        assert result["skipped"] is False

    def test_discover_chains_optimize(self, ingress_mod: ModuleType) -> None:
        opts = {
            "bbuddy_url": "http://bb",
            "bbuddy_user": "admin",
            "bbuddy_password": "pass",
            "store_id": "N110",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "gemini_api_key": "gem-key",
            "gemini_model": "gemini-1.5-flash",
            "location_id": 2,
            "quantity_unit_id": 3,
        }
        mock_grocy_cls = mock.MagicMock()
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                with mock.patch("grocy_scraper.grocy_client.GrocyClient", mock_grocy_cls):
                    main_mod = sys.modules["main"]
                    main_mod._discover_products.return_value = (0, [42, 99])
                    main_mod._ai_optimize_products.return_value = 5
                    result = ingress_mod._handle_discover()

        assert result["success"] is True
        assert result["skipped"] is False
        main_mod._discover_products.assert_called_once()
        main_mod._ai_optimize_products.assert_called_once()
        _, opt_kwargs = main_mod._ai_optimize_products.call_args
        assert opt_kwargs["product_ids"] == [42, 99]

    def test_discover_no_gemini_key_skips_ai(self, ingress_mod: ModuleType) -> None:
        opts = {
            "bbuddy_url": "http://bb",
            "bbuddy_user": "admin",
            "bbuddy_password": "pass",
            "store_id": "N110",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
        }
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                main_mod = sys.modules["main"]
                main_mod._discover_products.return_value = (0, [42])
                result = ingress_mod._handle_discover()

        assert result["success"] is True
        main_mod._ai_sort_products.assert_not_called()
        main_mod._ai_assign_due_dates.assert_not_called()
        main_mod._ai_group_products.assert_not_called()

    def test_discover_failure_skips_ai(self, ingress_mod: ModuleType) -> None:
        opts = {
            "bbuddy_url": "http://bb",
            "bbuddy_user": "admin",
            "bbuddy_password": "pass",
            "store_id": "N110",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "gemini_api_key": "gem-key",
        }
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                main_mod = sys.modules["main"]
                main_mod._discover_products.return_value = (1, [])
                result = ingress_mod._handle_discover()

        assert result["success"] is False
        main_mod._ai_sort_products.assert_not_called()
        main_mod._ai_assign_due_dates.assert_not_called()
        main_mod._ai_group_products.assert_not_called()

    def test_discover_no_new_products_skips_ai(self, ingress_mod: ModuleType) -> None:
        """When discover succeeds but finds no new products, AI is skipped."""
        opts = {
            "bbuddy_url": "http://bb",
            "bbuddy_user": "admin",
            "bbuddy_password": "pass",
            "store_id": "N110",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "gemini_api_key": "gem-key",
            "gemini_model": "gemini-1.5-flash",
            "location_id": 2,
            "quantity_unit_id": 3,
        }
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                main_mod = sys.modules["main"]
                main_mod._discover_products.return_value = (0, [])
                result = ingress_mod._handle_discover()

        assert result["success"] is True
        main_mod._ai_sort_products.assert_not_called()
        main_mod._ai_assign_due_dates.assert_not_called()
        main_mod._ai_group_products.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_sort / _handle_date
# ---------------------------------------------------------------------------


class TestHandleSort:
    def test_missing_gemini_key(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            result = ingress_mod._handle_sort()
        assert result["success"] is False
        assert result["skipped"] is True
        assert result["updated"] == 0

    def test_calls_ai_sort(self, ingress_mod: ModuleType) -> None:
        opts = {
            "gemini_api_key": "gem-key",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
        }
        mock_grocy_cls = mock.MagicMock()
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                with mock.patch("grocy_scraper.grocy_client.GrocyClient", mock_grocy_cls):
                    sys.modules["main"]._ai_sort_products.return_value = 5
                    result = ingress_mod._handle_sort()

        assert result["success"] is True
        assert result["updated"] == 5


class TestHandleDate:
    def test_missing_gemini_key(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            result = ingress_mod._handle_date()
        assert result["success"] is False
        assert result["skipped"] is True

    def test_calls_ai_date(self, ingress_mod: ModuleType) -> None:
        opts = {
            "gemini_api_key": "gem-key",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
        }
        mock_grocy_cls = mock.MagicMock()
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                with mock.patch("grocy_scraper.grocy_client.GrocyClient", mock_grocy_cls):
                    sys.modules["main"]._ai_assign_due_dates.return_value = 3
                    result = ingress_mod._handle_date()

        assert result["success"] is True
        assert result["updated"] == 3


# ---------------------------------------------------------------------------
# _handle_group
# ---------------------------------------------------------------------------


class TestHandleGroup:
    def test_missing_gemini_key(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            result = ingress_mod._handle_group()
        assert result["success"] is False
        assert result["skipped"] is True
        assert result["updated"] == 0

    def test_calls_ai_group(self, ingress_mod: ModuleType) -> None:
        opts = {
            "gemini_api_key": "gem-key",
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "location_id": 2,
            "quantity_unit_id": 3,
        }
        mock_grocy_cls = mock.MagicMock()
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                with mock.patch("grocy_scraper.grocy_client.GrocyClient", mock_grocy_cls):
                    sys.modules["main"]._ai_group_products.return_value = 7
                    result = ingress_mod._handle_group()

        assert result["success"] is True
        assert result["updated"] == 7


# ---------------------------------------------------------------------------
# _handle_update
# ---------------------------------------------------------------------------


class TestHandleUpdate:
    def test_calls_update_products(self, ingress_mod: ModuleType) -> None:
        opts = {"store_id": "N110", "grocy_url": "http://grocy", "grocy_api_key": "key"}
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch.dict(sys.modules, {"main": mock.MagicMock()}):
                sys.modules["main"]._update_products.return_value = 0
                result = ingress_mod._handle_update()

        assert result["success"] is True


# ---------------------------------------------------------------------------
# _handle_add_products
# ---------------------------------------------------------------------------


class TestHandleAddProducts:
    def test_empty_products(self, ingress_mod: ModuleType) -> None:
        result = ingress_mod._handle_add_products({})
        assert result["success"] is False
        assert "No products" in result["error"]

    def test_products_not_a_list(self, ingress_mod: ModuleType) -> None:
        result = ingress_mod._handle_add_products({"products": "bad"})
        assert result["success"] is False

    def test_missing_grocy_config(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "Test"}]}
            )
        assert result["success"] is False
        assert "Grocy URL" in result["error"]

    def test_adds_product_successfully(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "location_id": 2,
            "quantity_unit_id": 3,
        }
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.return_value = 42

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy
            ):
                result = ingress_mod._handle_add_products(
                    {
                        "products": [
                            {"name": "Maito 1L", "ean": "6411234000001", "description": "Kevytmaito"},
                        ]
                    }
                )

        assert result["success"] is True
        assert result["added"] == 1
        assert result["errors"] == []
        mock_grocy.create_product.assert_called_once_with(
            "Maito 1L", description="Kevytmaito", location_id=2, quantity_unit_id=3
        )
        mock_grocy.add_barcode.assert_called_once_with(42, "6411234000001")

    def test_skips_existing_barcode(self, ingress_mod: ModuleType) -> None:
        opts = {"grocy_url": "http://grocy", "grocy_api_key": "key"}
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = {"id": 1, "name": "Existing"}

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy
            ):
                result = ingress_mod._handle_add_products(
                    {"products": [{"name": "Existing", "ean": "111"}]}
                )

        assert result["success"] is True
        assert result["added"] == 0
        mock_grocy.create_product.assert_not_called()

    def test_handles_grocy_error(self, ingress_mod: ModuleType) -> None:
        from grocy_scraper.grocy_client import GrocyAPIError

        opts = {"grocy_url": "http://grocy", "grocy_api_key": "key"}
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.side_effect = GrocyAPIError("conflict")

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy
            ):
                result = ingress_mod._handle_add_products(
                    {"products": [{"name": "Bad", "ean": "222"}]}
                )

        assert result["success"] is False
        assert result["added"] == 0
        assert len(result["errors"]) == 1
        assert "conflict" in result["errors"][0]

    def test_skips_empty_name(self, ingress_mod: ModuleType) -> None:
        opts = {"grocy_url": "http://grocy", "grocy_api_key": "key"}
        mock_grocy = mock.MagicMock()

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy
            ):
                result = ingress_mod._handle_add_products(
                    {"products": [{"name": "", "ean": "333"}]}
                )

        assert result["added"] == 0
        mock_grocy.create_product.assert_not_called()

    def test_empty_product_list(self, ingress_mod: ModuleType) -> None:
        result = ingress_mod._handle_add_products({"products": []})
        assert result["success"] is False
        assert "No products" in result["error"]

    def test_unexpected_exception(self, ingress_mod: ModuleType) -> None:
        opts = {"grocy_url": "http://grocy", "grocy_api_key": "key"}
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.side_effect = RuntimeError("unexpected")

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy
            ):
                result = ingress_mod._handle_add_products(
                    {"products": [{"name": "Fail", "ean": "444"}]}
                )

        assert result["success"] is False
        assert result["added"] == 0
        assert len(result["errors"]) == 1
        assert "unexpected" in result["errors"][0]

    def test_uploads_image_when_image_url_present(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "upload_images": True,
        }
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.return_value = 10

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy), \
             mock.patch("main._upload_product_image") as mock_upload:
            result = ingress_mod._handle_add_products(
                {
                    "products": [
                        {
                            "name": "Juice",
                            "ean": "555",
                            "description": "Orange",
                            "image_url": "https://example.com/juice.jpg",
                        }
                    ]
                }
            )

        assert result["success"] is True
        assert result["added"] == 1
        mock_upload.assert_called_once()
        product_arg = mock_upload.call_args[0][0]
        assert product_arg.name == "Juice"
        assert product_arg.ean == "555"
        assert product_arg.image_url == "https://example.com/juice.jpg"
        assert mock_upload.call_args[0][1] is mock_grocy
        assert mock_upload.call_args[0][2] == 10

    def test_skips_image_upload_when_disabled(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "upload_images": False,
        }
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.return_value = 10

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy), \
             mock.patch("main._upload_product_image") as mock_upload:
            result = ingress_mod._handle_add_products(
                {
                    "products": [
                        {
                            "name": "Juice",
                            "ean": "555",
                            "image_url": "https://example.com/juice.jpg",
                        }
                    ]
                }
            )

        assert result["success"] is True
        assert result["added"] == 1
        mock_upload.assert_not_called()

    def test_skips_image_upload_when_no_image_url(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "upload_images": True,
        }
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.return_value = 10

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy), \
             mock.patch("main._upload_product_image") as mock_upload:
            result = ingress_mod._handle_add_products(
                {
                    "products": [
                        {"name": "Juice", "ean": "555"},
                    ]
                }
            )

        assert result["success"] is True
        assert result["added"] == 1
        mock_upload.assert_not_called()

    def test_chains_ai_sort_date_group_when_gemini_key_set(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "gemini_api_key": "gem-key",
            "gemini_model": "gemini-2.0-flash",
            "location_id": 2,
            "quantity_unit_id": 3,
        }
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.side_effect = [10, 20]

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy), \
             mock.patch("main._ai_optimize_products") as mock_optimize:
            result = ingress_mod._handle_add_products(
                {
                    "products": [
                        {"name": "A", "ean": "111"},
                        {"name": "B", "ean": "222"},
                    ]
                }
            )

        assert result["success"] is True
        assert result["added"] == 2

        mock_optimize.assert_called_once_with(
            mock_grocy,
            "gem-key",
            "gemini-2.0-flash",
            location_id=2,
            quantity_unit_id=3,
            product_ids=[10, 20],
        )

    def test_skips_ai_when_no_gemini_key(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
        }
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = None
        mock_grocy.create_product.return_value = 10

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy), \
             mock.patch("main._ai_optimize_products") as mock_optimize:
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "A", "ean": "111"}]}
            )

        assert result["success"] is True
        assert result["added"] == 1
        mock_optimize.assert_not_called()

    def test_skips_ai_when_no_products_added(self, ingress_mod: ModuleType) -> None:
        opts = {
            "grocy_url": "http://grocy",
            "grocy_api_key": "key",
            "gemini_api_key": "gem-key",
        }
        mock_grocy = mock.MagicMock()
        mock_grocy.get_product_by_barcode.return_value = {"id": 1, "name": "Existing"}

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("grocy_scraper.grocy_client.GrocyClient", return_value=mock_grocy), \
             mock.patch("main._ai_optimize_products") as mock_optimize:
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "Existing", "ean": "111"}]}
            )

        assert result["success"] is True
        assert result["added"] == 0
        mock_optimize.assert_not_called()


# ---------------------------------------------------------------------------
# HTTP handler (do_GET / do_POST)
# ---------------------------------------------------------------------------


class TestHTTPHandler:
    """Test the HTTP request handler routing."""

    def _make_handler(
        self,
        ingress_mod: ModuleType,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> tuple[int, dict | str]:
        """Create a mock handler and invoke do_GET or do_POST."""
        body_bytes = json.dumps(body).encode() if body is not None else b""

        handler = ingress_mod._Handler.__new__(ingress_mod._Handler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(body_bytes)), "Content-Type": "application/json"}
        handler.rfile = BytesIO(body_bytes)
        handler.wfile = BytesIO()
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)

        # Capture send_response / send_header / end_headers
        response_status = []
        headers_sent: dict[str, str] = {}

        def mock_send_response(code: int) -> None:
            response_status.append(code)

        def mock_send_header(key: str, val: str) -> None:
            headers_sent[key] = val

        def mock_end_headers() -> None:
            pass

        handler.send_response = mock_send_response
        handler.send_header = mock_send_header
        handler.end_headers = mock_end_headers

        if method == "GET":
            handler.do_GET()
        else:
            handler.do_POST()

        status = response_status[0] if response_status else 0
        output = handler.wfile.getvalue()

        # Try to parse as JSON
        try:
            return status, json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return status, output.decode()

    def test_get_root_serves_html(self, ingress_mod: ModuleType) -> None:
        status, body = self._make_handler(ingress_mod, "GET", "/")
        assert status == 200
        assert "Grocy Scraper" in body

    def test_get_api_config(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            status, body = self._make_handler(ingress_mod, "GET", "/api/config")
        assert status == 200
        assert body["configured"] is False

    def test_post_search_empty(self, ingress_mod: ModuleType) -> None:
        status, body = self._make_handler(ingress_mod, "POST", "/api/search", body={})
        assert status == 200
        assert body["status"] == "running"
        task_id = body["task_id"]
        time.sleep(0.1)
        status2, result = self._make_handler(ingress_mod, "GET", f"/api/task/{task_id}")
        assert status2 == 200
        assert result["success"] is False

    def test_post_unknown_endpoint(self, ingress_mod: ModuleType) -> None:
        status, body = self._make_handler(ingress_mod, "POST", "/api/unknown", body={})
        assert status == 404

    def test_post_invalid_json(self, ingress_mod: ModuleType) -> None:
        handler = ingress_mod._Handler.__new__(ingress_mod._Handler)
        handler.path = "/api/search"
        bad_body = b"not json"
        handler.headers = {"Content-Length": str(len(bad_body)), "Content-Type": "application/json"}
        handler.rfile = BytesIO(bad_body)
        handler.wfile = BytesIO()
        handler.requestline = "POST /api/search HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)

        response_status = []
        handler.send_response = lambda code: response_status.append(code)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None

        handler.do_POST()

        assert response_status[0] == 400

    def test_post_discover_no_config(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            status, body = self._make_handler(ingress_mod, "POST", "/api/discover")
        assert status == 200
        assert body["status"] == "running"
        task_id = body["task_id"]
        time.sleep(0.1)
        status2, result = self._make_handler(ingress_mod, "GET", f"/api/task/{task_id}")
        assert status2 == 200
        assert result["skipped"] is True

    def test_post_add_products_no_body(self, ingress_mod: ModuleType) -> None:
        status, body = self._make_handler(ingress_mod, "POST", "/api/add_products", body={})
        assert status == 200
        assert body["status"] == "running"
        task_id = body["task_id"]
        time.sleep(0.1)
        status2, result = self._make_handler(ingress_mod, "GET", f"/api/task/{task_id}")
        assert status2 == 200
        assert result["success"] is False
        assert "No products" in result["error"]


# ---------------------------------------------------------------------------
# HTML content checks
# ---------------------------------------------------------------------------


class TestHTMLContent:
    """Verify the HTML UI contains the required UI elements."""

    def test_has_search_input(self, ingress_mod: ModuleType) -> None:
        assert 'id="query"' in ingress_mod._HTML

    def test_has_search_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="search-btn"' in ingress_mod._HTML

    def test_has_discover_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="discover-btn"' in ingress_mod._HTML

    def test_has_sort_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="sort-btn"' in ingress_mod._HTML

    def test_has_date_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="date-btn"' in ingress_mod._HTML

    def test_has_update_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="update-btn"' in ingress_mod._HTML

    def test_has_group_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="group-btn"' in ingress_mod._HTML

    def test_has_terminal_pane(self, ingress_mod: ModuleType) -> None:
        assert 'id="terminal"' in ingress_mod._HTML

    def test_has_verbose_toggle(self, ingress_mod: ModuleType) -> None:
        assert 'id="verbose-toggle"' in ingress_mod._HTML

    def test_has_clear_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="clear-btn"' in ingress_mod._HTML

    def test_has_max_products_input(self, ingress_mod: ModuleType) -> None:
        assert 'id="max-products"' in ingress_mod._HTML

    def test_has_config_card(self, ingress_mod: ModuleType) -> None:
        assert 'id="config-card"' in ingress_mod._HTML

    def test_has_select_all_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="select-all-btn"' in ingress_mod._HTML

    def test_has_select_none_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="select-none-btn"' in ingress_mod._HTML

    def test_has_add_products_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="add-products-btn"' in ingress_mod._HTML

    def test_has_selection_toolbar(self, ingress_mod: ModuleType) -> None:
        assert 'id="selection-toolbar"' in ingress_mod._HTML

    def test_has_selection_count(self, ingress_mod: ModuleType) -> None:
        assert 'id="selection-count"' in ingress_mod._HTML

    def test_has_add_status(self, ingress_mod: ModuleType) -> None:
        assert 'id="add-status"' in ingress_mod._HTML
