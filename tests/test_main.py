"""Integration tests for the main CLI entry point."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from grocy_scraper_addon.main import main, parse_args, sync_product, _parse_store_ids
from grocy_scraper.scraper import Product
from grocy_scraper.storage_client import StorageAPIError, StorageClient


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_query_mode(self):
        args = parse_args(["--store", "N110", "--query", "maito",
                           "--storage-url", "https://grocy.example.com"])
        assert args.query == "maito"
        assert not args.browse

    def test_browse_mode(self):
        args = parse_args(["--store", "N110", "--browse",
                           "--storage-url", "https://grocy.example.com"])
        assert args.browse
        assert args.query is None

    def test_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            parse_args(["--store", "N110", "--browse", "--query", "x"])

    def test_dry_run_flag(self):
        args = parse_args(["--store", "N110", "--browse", "--dry-run"])
        assert args.dry_run

    def test_max_products(self):
        args = parse_args(["--store", "N110", "--browse",
                           "--storage-url", "u",
                           "--max-products", "10"])
        assert args.max_products == 10

    def test_graphql_default(self):
        """use_graphql should default to True."""
        args = parse_args(["--store", "N110", "--browse", "--dry-run"])
        assert args.use_graphql is True

    def test_no_graphql_flag(self):
        """--no-graphql should set use_graphql=False."""
        args = parse_args(["--store", "N110", "--browse", "--dry-run", "--no-graphql"])
        assert args.use_graphql is False

    def test_location_id_from_env(self):
        """--location-id default should be read from GROCY_LOCATION_ID as int."""
        with patch.dict("os.environ", {"GROCY_LOCATION_ID": "42"}):
            args = parse_args(["--store", "N110", "--browse", "--dry-run"])
        assert args.location_id == 42
        assert isinstance(args.location_id, int)

    def test_quantity_unit_id_from_env(self):
        """--quantity-unit-id default should be read from GROCY_QUANTITY_UNIT_ID as int."""
        with patch.dict("os.environ", {"GROCY_QUANTITY_UNIT_ID": "7"}):
            args = parse_args(["--store", "N110", "--browse", "--dry-run"])
        assert args.quantity_unit_id == 7
        assert isinstance(args.quantity_unit_id, int)

    def test_location_id_none_when_unset(self):
        """--location-id default should be None when env var is unset."""
        with patch.dict("os.environ", {}, clear=True):
            args = parse_args(["--store", "N110", "--browse", "--dry-run"])
        assert args.location_id is None


class TestEnvInt:
    """Unit tests for the _env_int helper."""

    def test_returns_int(self):
        from grocy_scraper_addon.main import _env_int
        with patch.dict("os.environ", {"MY_VAR": "5"}):
            assert _env_int("MY_VAR") == 5

    def test_returns_none_when_unset(self):
        from grocy_scraper_addon.main import _env_int
        with patch.dict("os.environ", {}, clear=True):
            assert _env_int("MY_VAR") is None

    def test_returns_none_for_empty_string(self):
        from grocy_scraper_addon.main import _env_int
        with patch.dict("os.environ", {"MY_VAR": ""}):
            assert _env_int("MY_VAR") is None

    def test_raises_on_non_numeric(self):
        from grocy_scraper_addon.main import _env_int
        with patch.dict("os.environ", {"MY_VAR": "abc"}):
            with pytest.raises(ValueError):
                _env_int("MY_VAR")


# ---------------------------------------------------------------------------
# sync_product
# ---------------------------------------------------------------------------

class TestSyncProduct:
    def _grocy(self):
        g = MagicMock(spec=StorageClient)
        g.get_product_by_barcode.return_value = None
        g.create_product.return_value = 99
        return g

    def test_skips_product_without_ean(self):
        grocy = self._grocy()
        product = Product(name="Tuote", ean="")
        result = sync_product(
            product, grocy,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=set()
        )
        assert result is False
        grocy.create_product.assert_not_called()

    def test_skips_known_barcode(self):
        grocy = self._grocy()
        product = Product(name="Maito", ean="111")
        known = {"111"}
        result = sync_product(
            product, grocy,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=known
        )
        assert result is False
        grocy.create_product.assert_not_called()

    def test_creates_new_product(self):
        grocy = self._grocy()
        product = Product(name="Maito", ean="999")
        result = sync_product(
            product, grocy,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=set()
        )
        assert result is True
        grocy.create_product.assert_called_once_with(
            name="Maito", description="", location_id=None, unit_id=None
        )
        grocy.add_barcode.assert_called_once_with(99, "999")

    def test_skips_existing_barcode_from_grocy_api(self):
        grocy = self._grocy()
        grocy.get_product_by_barcode.return_value = {"id": 5, "name": "Existing"}
        product = Product(name="Maito", ean="555")
        result = sync_product(
            product, grocy,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=set()
        )
        assert result is False
        grocy.create_product.assert_not_called()

    def test_create_product_error_returns_false(self):
        grocy = self._grocy()
        grocy.create_product.side_effect = StorageAPIError("fail")
        product = Product(name="Voi", ean="777")
        result = sync_product(
            product, grocy,
            location_id=None, quantity_unit_id=None,
            skip_existing=False, known_barcodes=set()
        )
        assert result is False

    @patch("grocy_scraper_addon.main.requests.get")
    def test_uploads_image_when_enabled(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8image-data"
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        grocy = self._grocy()
        product = Product(name="Maito", ean="123", image_url="https://img.example.com/photo.jpg")
        sync_product(
            product, grocy,
            location_id=1, quantity_unit_id=1,
            skip_existing=False, known_barcodes=set(),
            upload_images=True,
        )
        grocy.upload_product_image.assert_called_once_with(
            99, "123.jpg", b"\xff\xd8image-data", content_type="image/jpeg"
        )

    def test_no_image_upload_by_default(self):
        grocy = self._grocy()
        product = Product(name="Maito", ean="123", image_url="https://img.example.com/photo.jpg")
        sync_product(
            product, grocy,
            location_id=1, quantity_unit_id=1,
            skip_existing=False, known_barcodes=set(),
        )
        grocy.upload_product_image.assert_not_called()

    @patch("grocy_scraper_addon.main.requests.get")
    def test_image_download_failure_does_not_break_sync(self, mock_get):
        import requests as req
        mock_get.side_effect = req.RequestException("timeout")

        grocy = self._grocy()
        product = Product(name="Maito", ean="123", image_url="https://img.example.com/photo.jpg")
        result = sync_product(
            product, grocy,
            location_id=1, quantity_unit_id=1,
            skip_existing=False, known_barcodes=set(),
            upload_images=True,
        )
        assert result is True
        grocy.create_product.assert_called_once()
        grocy.upload_product_image.assert_not_called()


# ---------------------------------------------------------------------------
# _image_extension helper
# ---------------------------------------------------------------------------

class TestImageExtension:
    def test_known_mime_types(self):
        from grocy_scraper_addon.main import _image_extension
        assert _image_extension("image/jpeg", "") == ".jpg"
        assert _image_extension("image/png", "") == ".png"
        assert _image_extension("image/webp", "") == ".webp"

    def test_unknown_mime_falls_back_to_url(self):
        from grocy_scraper_addon.main import _image_extension
        assert _image_extension("application/octet-stream", "https://example.com/img.png?w=100") == ".png"

    def test_unknown_mime_no_ext_defaults_to_jpg(self):
        from grocy_scraper_addon.main import _image_extension
        assert _image_extension("application/octet-stream", "https://example.com/img") == ".jpg"


# ---------------------------------------------------------------------------
# main – dry run
# ---------------------------------------------------------------------------

class TestMainDryRun:
    def test_dry_run_missing_store(self, capsys):
        rc = main(["--browse", "--dry-run"])
        assert rc == 1

    def test_dry_run_prints_products(self, capsys):
        products = [
            Product(name="Maito", ean="111"),
            Product(name="Kerma", ean="222"),
        ]
        with patch("grocy_scraper_addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter(products)
            rc = main(["--store", "N110", "--browse", "--dry-run"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Maito" in out
        assert "111" in out

    def test_dry_run_passes_use_graphql_true(self, capsys):
        """By default, KRuokaScraper is constructed with use_graphql=True."""
        with patch("grocy_scraper_addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter([])
            main(["--store", "N110", "--browse", "--dry-run"])

        _, kwargs = MockScraper.call_args
        assert kwargs.get("use_graphql", True) is True

    def test_no_graphql_flag_passes_false(self, capsys):
        """--no-graphql passes use_graphql=False to KRuokaScraper."""
        with patch("grocy_scraper_addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter([])
            main(["--store", "N110", "--browse", "--dry-run", "--no-graphql"])

        _, kwargs = MockScraper.call_args
        assert kwargs.get("use_graphql") is False


# ---------------------------------------------------------------------------
# main – missing Grocy config
# ---------------------------------------------------------------------------

class TestMainMissingStorage:
    def test_missing_storage_url(self, capsys):
        rc = main(["--store", "N110", "--browse"])
        assert rc == 1


# ---------------------------------------------------------------------------
# parse_args – AI flags
# ---------------------------------------------------------------------------

class TestParseArgsAIFlags:
    def test_sort_flag(self):
        args = parse_args([
            "--sort",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.sort is True
        assert args.date is False
        assert args.gemini_api_key == "GEMINI_KEY"

    def test_date_flag(self):
        args = parse_args([
            "--date",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.date is True
        assert args.sort is False

    def test_sort_and_date_together(self):
        args = parse_args([
            "--sort", "--date",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.sort is True
        assert args.date is True

    def test_group_flag(self):
        args = parse_args([
            "--group",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.group is True
        assert args.sort is False
        assert args.date is False

    def test_group_default_false(self):
        args = parse_args(["--store", "N110", "--browse", "--dry-run"])
        assert args.group is False

    def test_gemini_key_from_env(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API", "env-key-123")
        args = parse_args([
            "--sort",
            "--storage-url", "https://grocy.example.com",
        ])
        assert args.gemini_api_key == "env-key-123"

    def test_gemini_model_default(self):
        from grocy_scraper_addon.main import _GEMINI_DEFAULT_MODEL
        args = parse_args([
            "--sort",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "KEY",
        ])
        assert args.gemini_model == _GEMINI_DEFAULT_MODEL

    def test_gemini_model_flag(self):
        args = parse_args([
            "--sort",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "KEY",
            "--gemini-model", "gemini-2.0-flash",
        ])
        assert args.gemini_model == "gemini-2.0-flash"

    def test_gemini_model_from_env(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-pro")
        args = parse_args([
            "--sort",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "KEY",
        ])
        assert args.gemini_model == "gemini-2.0-pro"


# ---------------------------------------------------------------------------
# _validate_args – AI mode validation
# ---------------------------------------------------------------------------

class TestValidateArgsAI:
    def _base_ai_args(self, **overrides):
        from argparse import Namespace
        defaults = dict(
            sort=True, date=False, group=False, optimize=False,
            storage_url="https://grocy.example.com",
            gemini_api_key="GEMINI_KEY",
            query=None, browse=False,
            discover=False, delete_all=False, update=False,
            dry_run=False,
            store="",
            location_id=None,
            quantity_unit_id=None,
        )
        defaults.update(overrides)
        return Namespace(**defaults)

    def test_valid_sort_passes(self):
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_ai_args()) == 0

    def test_missing_gemini_key_fails(self):
        from grocy_scraper_addon.main import _validate_args
        args = self._base_ai_args(gemini_api_key="")
        assert _validate_args(args) == 1

    def test_missing_storage_url_fails(self):
        from grocy_scraper_addon.main import _validate_args
        args = self._base_ai_args(storage_url="")
        assert _validate_args(args) == 1

    def test_no_mode_fails(self):
        from grocy_scraper_addon.main import _validate_args
        from argparse import Namespace
        args = Namespace(
            sort=False, date=False, group=False, optimize=False,
            query=None, browse=False,
            discover=False, delete_all=False, update=False,
            dry_run=False,
            storage_url="", gemini_api_key="",
            store="", location_id=None, quantity_unit_id=None,
        )
        assert _validate_args(args) == 1

    def test_dry_run_alone_fails(self):
        from grocy_scraper_addon.main import _validate_args
        from argparse import Namespace
        args = Namespace(
            sort=False, date=False, group=False, optimize=False,
            query=None, browse=False,
            discover=False, delete_all=False, update=False,
            dry_run=True,
            storage_url="", gemini_api_key="",
            store="N110", location_id=None, quantity_unit_id=None,
        )
        assert _validate_args(args) == 1


# ---------------------------------------------------------------------------
# _call_gemini
# ---------------------------------------------------------------------------

class TestCallGemini:
    @patch("grocy_scraper_addon.main.requests.post")
    def test_returns_text_from_response(self, mock_post):
        from grocy_scraper_addon.main import _call_gemini
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '{"1": 2}'}]}}]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        result = _call_gemini("prompt", "api-key")
        assert result == '{"1": 2}'
        _, kwargs = mock_post.call_args
        assert kwargs["params"]["key"] == "api-key"

    @patch("grocy_scraper_addon.main.requests.post")
    def test_uses_specified_model_in_url(self, mock_post):
        from grocy_scraper_addon.main import _call_gemini
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        _call_gemini("prompt", "api-key", model="gemini-2.0-flash")
        url = mock_post.call_args[0][0]
        assert "gemini-2.0-flash" in url

    @patch("grocy_scraper_addon.main.requests.post")
    def test_http_error_raises(self, mock_post):
        import requests as req
        from grocy_scraper_addon.main import _call_gemini
        mock_resp = MagicMock(status_code=400)
        mock_post.return_value = mock_resp
        mock_resp.raise_for_status.side_effect = req.HTTPError(response=mock_resp)
        with pytest.raises(StorageAPIError, match="Gemini API error"):
            _call_gemini("prompt", "bad-key")

    @patch("grocy_scraper_addon.main.requests.post")
    def test_unexpected_format_raises(self, mock_post):
        from grocy_scraper_addon.main import _call_gemini
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}  # missing 'candidates'
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        with pytest.raises(StorageAPIError, match="Unexpected Gemini"):
            _call_gemini("prompt", "key")


class TestCallGeminiJson:
    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_returns_parsed_json(self, mock_gemini, _mock_sleep):
        from grocy_scraper_addon.main import _call_gemini_json
        mock_gemini.return_value = '{"1": 2}'
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        _mock_sleep.assert_not_called()

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_sanitizes_control_characters(self, mock_gemini, _mock_sleep):
        from grocy_scraper_addon.main import _call_gemini_json
        mock_gemini.return_value = '{"1":\x02 2}'
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        _mock_sleep.assert_not_called()

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_retries_on_json_error(self, mock_gemini, mock_sleep):
        from grocy_scraper_addon.main import _call_gemini_json
        mock_gemini.side_effect = ["not-json", '{"1": 2}']
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        assert mock_gemini.call_count == 2
        mock_sleep.assert_called_once()

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_retries_on_api_error(self, mock_gemini, mock_sleep):
        from grocy_scraper_addon.main import _call_gemini_json
        mock_gemini.side_effect = [StorageAPIError("HTML error"), '{"1": 2}']
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        assert mock_gemini.call_count == 2
        mock_sleep.assert_called_once()

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_raises_after_max_retries(self, mock_gemini, mock_sleep):
        import json as _json
        from grocy_scraper_addon.main import _call_gemini_json
        mock_gemini.return_value = "not-json"
        with pytest.raises(_json.JSONDecodeError):
            _call_gemini_json("prompt", "key", max_retries=3)
        assert mock_gemini.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_exponential_backoff(self, mock_gemini, mock_sleep):
        import json as _json
        from grocy_scraper_addon.main import _call_gemini_json
        mock_gemini.return_value = "bad"
        with pytest.raises(_json.JSONDecodeError):
            _call_gemini_json("prompt", "key", max_retries=3)
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        assert delays == [2, 4]


# ---------------------------------------------------------------------------
# _ai_sort_products
# ---------------------------------------------------------------------------

class TestAiSortProducts:
    def _make_grocy(self, products, locations):
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.get_locations.return_value = locations
        g.update_product.return_value = None
        g.get_product_stock_locations.return_value = []
        g.transfer_stock.return_value = None
        return g

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_updates_products_with_ai_locations(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}, {"id": 2, "name": "Pesuaine"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 3, "name": "Cleaning Cabinet"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2, "2": 3}'

        result = _ai_sort_products(grocy, "gemini-key")
        assert result == 2
        grocy.update_product.assert_any_call(1, location_id=2)
        grocy.update_product.assert_any_call(2, location_id=3)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_transfers_stock_to_new_location(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 5, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2}'
        grocy.get_product_stock_locations.return_value = [
            {"location_id": 5, "amount": 3.0},
        ]

        result = _ai_sort_products(grocy, "gemini-key")
        assert result == 1
        grocy.transfer_stock.assert_called_once_with(1, 3.0, 5, 2)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_skips_transfer_when_stock_already_at_target(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2}'
        grocy.get_product_stock_locations.return_value = [
            {"location_id": 2, "amount": 4.0},
        ]

        _ai_sort_products(grocy, "gemini-key")
        grocy.transfer_stock.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_transfers_from_multiple_locations(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 5, "name": "Pantry"}, {"id": 6, "name": "Counter"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2}'
        grocy.get_product_stock_locations.return_value = [
            {"location_id": 5, "amount": 2.0},
            {"location_id": 6, "amount": 1.0},
            {"location_id": 2, "amount": 3.0},
        ]

        _ai_sort_products(grocy, "gemini-key")
        assert grocy.transfer_stock.call_count == 2
        grocy.transfer_stock.assert_any_call(1, 2.0, 5, 2)
        grocy.transfer_stock.assert_any_call(1, 1.0, 6, 2)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_transfer_error_does_not_abort(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}, {"id": 2, "name": "Pasta"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 5, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2, "2": 5}'
        grocy.get_product_stock_locations.side_effect = [
            [{"location_id": 5, "amount": 1.0}],
            [{"location_id": 2, "amount": 1.0}],
        ]
        grocy.transfer_stock.side_effect = [StorageAPIError("fail"), None]

        result = _ai_sort_products(grocy, "gemini-key")
        assert result == 2
        assert grocy.transfer_stock.call_count == 2

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_stock_locations_error_continues(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}, {"id": 2, "name": "Pasta"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 5, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2, "2": 5}'
        grocy.get_product_stock_locations.side_effect = [
            StorageAPIError("fail"),
            [],
        ]

        result = _ai_sort_products(grocy, "gemini-key")
        assert result == 2
        grocy.transfer_stock.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_no_locations_returns_zero(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        grocy = MagicMock(spec=StorageClient)
        grocy.get_locations.return_value = []
        result = _ai_sort_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_no_products_returns_zero(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        grocy = MagicMock(spec=StorageClient)
        grocy.get_locations.return_value = [{"id": 2, "name": "Fridge"}]
        grocy.get_all_products.return_value = []
        result = _ai_sort_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini, _mock_sleep):
        from grocy_scraper_addon.main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = "not-json"

        result = _ai_sort_products(grocy, "key")
        assert result == 0
        grocy.update_product.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_grocy_error_on_locations_returns_zero(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_sort_products
        grocy = MagicMock(spec=StorageClient)
        grocy.get_locations.side_effect = StorageAPIError("fail")
        result = _ai_sort_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()


# ---------------------------------------------------------------------------
# _ai_assign_due_dates
# ---------------------------------------------------------------------------

class TestAiAssignDueDates:
    def _make_grocy(self, products):
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.update_product.return_value = None
        return g

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_updates_products_with_ai_dates(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_assign_due_dates
        products = [{"id": 1, "name": "Maito"}, {"id": 5, "name": "Pasta"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = '{"1": 14, "5": 1095}'

        result = _ai_assign_due_dates(grocy, "gemini-key")
        assert result == 2
        grocy.update_product.assert_any_call(1, default_best_before_days=14)
        grocy.update_product.assert_any_call(5, default_best_before_days=1095)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_no_products_returns_zero(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_assign_due_dates
        grocy = MagicMock(spec=StorageClient)
        grocy.get_all_products.return_value = []
        result = _ai_assign_due_dates(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini, _mock_sleep):
        from grocy_scraper_addon.main import _ai_assign_due_dates
        products = [{"id": 1, "name": "Maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = "not-json"

        result = _ai_assign_due_dates(grocy, "key")
        assert result == 0
        grocy.update_product.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_grocy_error_returns_zero(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_assign_due_dates
        grocy = MagicMock(spec=StorageClient)
        grocy.get_all_products.side_effect = StorageAPIError("fail")
        result = _ai_assign_due_dates(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()


# ---------------------------------------------------------------------------
# _deduplicate_parent_products
# ---------------------------------------------------------------------------

class TestDeduplicateParentProducts:

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_merges_synonym_parents(self, mock_gemini):
        """Synonym parents are merged: children moved, duplicates deleted."""
        from grocy_scraper_addon.main import _deduplicate_parent_products
        products = [
            {"id": 10, "name": "Mausteet"},
            {"id": 11, "name": "Mauste"},
            {"id": 12, "name": "Mausteseos"},
            {"id": 100, "name": "Curry", "parent_id": 10},
            {"id": 101, "name": "Chili", "parent_id": 11},
            {"id": 102, "name": "Garam Masala", "parent_id": 12},
            {"id": 20, "name": "Leipä"},
            {"id": 200, "name": "Ruisleipä", "parent_id": 20},
        ]
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.update_product.return_value = None
        g.delete_product.return_value = None
        # Gemini maps all spice variants → "Mausteet", Leipä → itself.
        mock_gemini.return_value = (
            '{"Mausteet": "Mausteet", "Mauste": "Mausteet", '
            '"Mausteseos": "Mausteet", "Leipä": "Leipä"}'
        )

        result = _deduplicate_parent_products(g, "gemini-key")

        assert result == (2, {"Mauste": "Mausteet", "Mausteseos": "Mausteet"})
        # Children of non-canonical parents should be moved.
        g.update_product.assert_any_call(101, parent_id=10)
        g.update_product.assert_any_call(102, parent_id=10)
        # Non-canonical parents should be deleted.
        g.delete_product.assert_any_call(11)
        g.delete_product.assert_any_call(12)
        # Canonical parent 10 ("Mausteet") should NOT be deleted.
        assert all(c.args != (10,) for c in g.delete_product.call_args_list)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_noop_when_no_duplicates(self, mock_gemini):
        """When all parents are unique, nothing is merged."""
        from grocy_scraper_addon.main import _deduplicate_parent_products
        products = [
            {"id": 10, "name": "Mausteet"},
            {"id": 20, "name": "Leipä"},
            {"id": 100, "name": "Curry", "parent_id": 10},
            {"id": 200, "name": "Ruisleipä", "parent_id": 20},
        ]
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        mock_gemini.return_value = '{"Mausteet": "Mausteet", "Leipä": "Leipä"}'

        result = _deduplicate_parent_products(g, "gemini-key")

        assert result == (0, {})

    def test_skips_when_fewer_than_two_parents(self):
        """No Gemini call when only 0 or 1 parent exists."""
        from grocy_scraper_addon.main import _deduplicate_parent_products
        products = [
            {"id": 10, "name": "Mausteet"},
            {"id": 100, "name": "Curry", "parent_id": 10},
        ]
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products

        result = _deduplicate_parent_products(g, "gemini-key")

        assert result == (0, {})

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_handles_gemini_failure_gracefully(self, mock_gemini):
        """Dedup returns (0, {}) and doesn't crash on Gemini failure."""
        from grocy_scraper_addon.main import _deduplicate_parent_products
        products = [
            {"id": 10, "name": "Mausteet"},
            {"id": 11, "name": "Mauste"},
            {"id": 100, "name": "Curry", "parent_id": 10},
            {"id": 101, "name": "Chili", "parent_id": 11},
        ]
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        mock_gemini.side_effect = StorageAPIError("rate limit")

        result = _deduplicate_parent_products(g, "gemini-key")

        assert result == (0, {})

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_cascade_deletes_parent_without_image_cleanup(self, mock_gemini):
        """Parent product deletion relies on CASCADE (no manual image cleanup)."""
        from grocy_scraper_addon.main import _deduplicate_parent_products
        products = [
            {"id": 10, "name": "Mausteet"},
            {"id": 11, "name": "Mauste", "picture_filename": "mauste.jpg"},
            {"id": 100, "name": "Curry", "parent_id": 10},
            {"id": 101, "name": "Chili", "parent_id": 11},
        ]
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.update_product.return_value = None
        g.delete_product.return_value = None
        g.delete_product_image.return_value = None
        mock_gemini.return_value = '{"Mausteet": "Mausteet", "Mauste": "Mausteet"}'

        result = _deduplicate_parent_products(g, "gemini-key")

        assert result == (1, {"Mauste": "Mausteet"})
        # CASCADE handles cleanup — no manual image deletion.
        g.delete_product_image.assert_not_called()
        g.delete_product.assert_called_once_with(11)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_redirect_map_used_by_group(self, mock_gemini):
        """_ai_group_products redirects merged-away names via dedup map."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 10, "name": "Makeiset"},
            {"id": 100, "name": "Curry", "parent_id": 10},
            {"id": 50, "name": "Suklaapatukka"},
        ]
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.update_product.return_value = None
        g.ensure_product_group.return_value = 60
        g.get_product_groups.return_value = []
        # Gemini suggests "Karkki" parent (was merged into "Makeiset" by dedup).
        mock_gemini.return_value = (
            '{"50": {"parent": "Karkki", "category": "Makeiset"}}'
        )

        with patch(
            "grocy_scraper_addon.main._deduplicate_parent_products",
            return_value=(1, {"Karkki": "Makeiset"}),
        ):
            _ai_group_products(g, "gemini-key", product_ids=[50])

        # Should use existing "Makeiset" (ID 10), NOT create new "Karkki".
        g.create_product.assert_not_called()
        g.update_product.assert_any_call(50, parent_id=10, product_group_id=60)

    @patch("grocy_scraper_addon.main._fix_broken_product_units", return_value=0)
    @patch("grocy_scraper_addon.main._ai_detect_package_sizes", return_value=0)
    @patch("grocy_scraper_addon.main._ensure_units_and_conversions", return_value={"piece": 1})
    @patch("grocy_scraper_addon.main._call_gemini_json")
    def test_incremental_optimize_skips_dedup(self, mock_gemini_json, _m_ens, _m_pkg, _m_fix):
        """Incremental _ai_optimize_products skips heavy dedup."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 10, "name": "Makeiset",
             "active": False},
            {"id": 100, "name": "Curry", "parent_id": 10},
            {"id": 50, "name": "Suklaapatukka"},
        ]
        locations = [{"id": 1, "name": "Fridge"}]
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.get_locations.return_value = locations
        g.update_product.return_value = None
        g.ensure_product_group.return_value = 60
        g.get_product_groups.return_value = []
        g.get_product_stock_locations.return_value = []
        g.create_product.return_value = 999
        mock_gemini_json.return_value = {
            "50": {
                "location_id": 1,
                "best_before_days": 365,
                "group_name": "Karkki",
                "category": "Makeiset",
                "pack_size": None,
                "pack_unit": None,
            },
        }

        with patch(
            "grocy_scraper_addon.main._deduplicate_parent_products",
        ) as mock_dedup:
            _ai_optimize_products(g, "gemini-key", product_ids=[50])
            # Dedup should NOT be called in incremental mode.
            mock_dedup.assert_not_called()


# ---------------------------------------------------------------------------
# _ai_group_products
# ---------------------------------------------------------------------------

@patch("grocy_scraper_addon.main._deduplicate_parent_products", return_value=(0, {}))
class TestAiGroupProducts:
    _GROUP_MASTER_ID = 50
    _CATEGORY_GROUP_ID = 60

    def _make_grocy(self, products):
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.update_product.return_value = None
        g.create_product.return_value = 100
        g.get_product_groups.return_value = []
        g.delete_product.return_value = None
        g.delete_product_image.return_value = None
        g.delete_product_group.return_value = None
        # First call → "Group master"; subsequent calls → category groups.
        g.ensure_product_group.side_effect = (
            lambda name: self._GROUP_MASTER_ID
            if name == "Group master"
            else self._CATEGORY_GROUP_ID
        )
        return g

    # -- Full-mode tests (product_ids=None, clean-slate) -------------------

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_groups_products_under_new_parent(self, mock_gemini, _mock_dedup):
        """Full mode: simple grouping of ungrouped products."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka kevytmaito 1l"},
            {"id": 2, "name": "Valio kevytmaito 1l"},
            {"id": 3, "name": "Fazer ruisleipä"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maitotaloustuotteet"}, '
            '"2": {"parent": "Maito", "category": "Maitotaloustuotteet"}, '
            '"3": null}'
        )

        result = _ai_group_products(grocy, "gemini-key")
        assert result == 2
        # Dedup NOT called in full mode.
        _mock_dedup.assert_not_called()
        # "Group master" and category product groups should be ensured.
        grocy.ensure_product_group.assert_any_call("Group master")
        grocy.ensure_product_group.assert_any_call("Maitotaloustuotteet")
        # Parent product "Maito" should be created (not in existing products).
        grocy.create_product.assert_called_once_with(
            "Maito", location_id=None, unit_id=None,
        )
        # Parent should be configured with stock accumulation, "Group master"
        # product group, and hidden from the stock overview.
        grocy.update_product.assert_any_call(
            100,
            product_group_id=self._GROUP_MASTER_ID,
        )
        # Child products should be updated with parent_id and the
        # broad category product group.
        grocy.update_product.assert_any_call(
            1, parent_id=100, product_group_id=self._CATEGORY_GROUP_ID,
        )
        grocy.update_product.assert_any_call(
            2, parent_id=100, product_group_id=self._CATEGORY_GROUP_ID,
        )

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_reuses_existing_parent_product(self, mock_gemini, _mock_dedup):
        """Full mode: existing product is reused as parent."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 10, "name": "Maito"},  # Already exists as potential parent.
            {"id": 11, "name": "Pirkka kevytmaito 1l"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"10": null, "11": {"parent": "Maito", "category": "Maitotaloustuotteet"}}'
        )

        result = _ai_group_products(grocy, "gemini-key")
        assert result == 1
        # Should NOT create a new product — reuse existing "Maito".
        grocy.create_product.assert_not_called()
        # Child should be assigned parent and category product group.
        grocy.update_product.assert_any_call(
            11, parent_id=10, product_group_id=self._CATEGORY_GROUP_ID,
        )
        # Existing parent should still be updated with group / hide flags.
        grocy.update_product.assert_any_call(
            10,
            product_group_id=self._GROUP_MASTER_ID,
        )

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_strips_parents_and_sends_all(self, mock_gemini, _mock_dedup):
        """Full mode: existing parent assignments are stripped first and
        ALL products (except old parent placeholders) go to Gemini."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka maito", "parent_id": 99},
            {"id": 2, "name": "Valio maito"},
            {"id": 99, "name": "OldParent",
             "active": False},
        ]
        grocy = self._make_grocy(products)
        # After regrouping, OldParent has no new children.
        grocy.get_all_products.side_effect = [
            products,
            # Cleanup call — OldParent has no children.
            [
                {"id": 1, "name": "Pirkka maito", "parent_id": 100},
                {"id": 2, "name": "Valio maito", "parent_id": 100},
                {"id": 99, "name": "OldParent",
                 "active": False},
                {"id": 100, "name": "Maito"},
            ],
            # PG cleanup call.
            [
                {"id": 1, "name": "Pirkka maito", "parent_id": 100,
                 "product_group_id": self._CATEGORY_GROUP_ID},
                {"id": 2, "name": "Valio maito", "parent_id": 100,
                 "product_group_id": self._CATEGORY_GROUP_ID},
                {"id": 100, "name": "Maito",
                 "product_group_id": self._GROUP_MASTER_ID},
            ],
        ]
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maitotaloustuotteet"}, '
            '"2": {"parent": "Maito", "category": "Maitotaloustuotteet"}}'
        )

        result = _ai_group_products(grocy, "gemini-key")
        # Parent stripping: product 1 had its parent removed.
        grocy.update_product.assert_any_call(1, parent_id="")
        # Both products sent to Gemini.
        prompt_text = mock_gemini.call_args[0][0]
        assert "Pirkka maito" in prompt_text
        assert "Valio maito" in prompt_text
        # Old parent placeholder (99) should NOT be in the prompt.
        assert "OldParent" not in prompt_text
        # Old parent 99 should be deleted (no new children).
        grocy.delete_product.assert_any_call(99)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_cleans_unused_product_groups(self, mock_gemini, _mock_dedup):
        """Full mode: product groups with no products assigned are deleted."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [{"id": 1, "name": "Maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Maitotuote", "category": "Maitotaloustuotteet"}}'
        )
        # PG cleanup calls return stale group.
        grocy.get_all_products.side_effect = [
            products,
            # PG cleanup: product has new group, but old group "Vanhat" has no products.
            [{"id": 1, "name": "Maito",
              "product_group_id": self._CATEGORY_GROUP_ID}],
        ]
        grocy.get_product_groups.return_value = [
            {"id": self._CATEGORY_GROUP_ID, "name": "Maitotaloustuotteet"},
            {"id": 70, "name": "Vanhat"},
            {"id": self._GROUP_MASTER_ID, "name": "Group master"},
        ]

        _ai_group_products(grocy, "gemini-key")
        # Unused "Vanhat" should be deleted; "Maitotaloustuotteet" and
        # "Group master" should be kept.
        grocy.delete_product_group.assert_called_once_with(70)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_no_products_returns_zero(self, mock_gemini, _mock_dedup):
        from grocy_scraper_addon.main import _ai_group_products
        grocy = MagicMock(spec=StorageClient)
        grocy.get_all_products.return_value = []
        result = _ai_group_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("grocy_scraper_addon.main.time.sleep")
    @patch("grocy_scraper_addon.main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini, _mock_sleep, _mock_dedup):
        from grocy_scraper_addon.main import _ai_group_products
        products = [{"id": 1, "name": "Maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = "not-json"

        result = _ai_group_products(grocy, "key")
        assert result == 0

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_grocy_error_returns_zero(self, mock_gemini, _mock_dedup):
        from grocy_scraper_addon.main import _ai_group_products
        grocy = MagicMock(spec=StorageClient)
        grocy.get_all_products.side_effect = StorageAPIError("fail")
        result = _ai_group_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_does_not_set_product_as_own_parent(self, mock_gemini, _mock_dedup):
        """If Gemini maps a product to a parent with the same name, skip it."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [{"id": 5, "name": "Maito"}]
        grocy = self._make_grocy(products)
        # Gemini says product 5 should be under "Maito", but that IS product 5.
        mock_gemini.return_value = (
            '{"5": {"parent": "Maito", "category": "Maitotaloustuotteet"}}'
        )

        result = _ai_group_products(grocy, "key")
        assert result == 0
        # update_product should be called for cumulate flag but not for parent assignment.
        calls = [c for c in grocy.update_product.call_args_list
                 if "parent_id" in (c.kwargs or {})]
        assert len(calls) == 0

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_passes_location_and_quantity_unit(self, mock_gemini, _mock_dedup):
        from grocy_scraper_addon.main import _ai_group_products
        products = [{"id": 1, "name": "Pirkka maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maitotaloustuotteet"}}'
        )

        _ai_group_products(grocy, "key", location_id=5, quantity_unit_id=3)
        grocy.create_product.assert_called_once_with(
            "Maito", location_id=5, unit_id=3,
        )

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_group_without_product_group_on_ensure_failure(self, mock_gemini, _mock_dedup):
        """If ensure_product_group fails, grouping still works without group IDs."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka maito"},
            {"id": 2, "name": "Valio maito"},
        ]
        grocy = self._make_grocy(products)
        grocy.ensure_product_group.side_effect = StorageAPIError("fail")
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maitotaloustuotteet"}, '
            '"2": {"parent": "Maito", "category": "Maitotaloustuotteet"}}'
        )

        result = _ai_group_products(grocy, "key")
        assert result == 2
        # Parent should NOT be updated (no group master, no flags to set).
        update_calls = [c for c in grocy.update_product.call_args_list if c[0] == (100,)]
        assert len(update_calls) == 0, f"Unexpected parent update: {update_calls}"
        # Children should be updated without product_group_id.
        grocy.update_product.assert_any_call(1, parent_id=100)
        grocy.update_product.assert_any_call(2, parent_id=100)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_no_existing_hints_in_prompt(self, mock_gemini, _mock_dedup):
        """Full mode: prompt does NOT include existing parent/category hints."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 10, "name": "Sipuli"},
            {"id": 11, "name": "Punasipuli", "parent_id": 10},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"10": null, "11": {"parent": "Sipuli", "category": "Vihannekset"}}'
        )

        _ai_group_products(grocy, "gemini-key")
        prompt_text = mock_gemini.call_args[0][0]
        assert "Existing parent products" not in prompt_text
        assert "Existing product categories" not in prompt_text

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_skips_existing_parent_that_is_already_a_child(self, mock_gemini, _mock_dedup):
        """If an existing product matching the parent name is already a child
        of another product, it must not be reused as a parent."""
        from grocy_scraper_addon.main import _ai_group_products
        # Product 1 "Sipuli" is a child of 99 but 99 is NOT an old parent
        # placeholder (no cumulate/hide flags), so 1 stays in candidates
        # but its parent is stripped. After stripping, it can be reused.
        products = [
            {"id": 1, "name": "Sipuli", "parent_id": 99},
            {"id": 2, "name": "Valkosipuli"},
            {"id": 99, "name": "Juurekset"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": null, "2": {"parent": "Sipuli", "category": "Vihannekset"}, '
            '"99": null}'
        )

        result = _ai_group_products(grocy, "gemini-key")
        # After stripping, "Sipuli" has no parent_id in memory,
        # so it CAN be reused as a parent for "Valkosipuli".
        assert result == 1
        grocy.update_product.assert_any_call(
            2, parent_id=1, product_group_id=self._CATEGORY_GROUP_ID,
        )

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_skips_parent_for_min_stock_product(self, mock_gemini, _mock_dedup):
        """Full mode: products with min_stock_amount > 0 keep no parent but get category."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 1, "name": "Mustapippuri 100g", "min_stock_amount": 1},
            {"id": 2, "name": "Oregano 50g"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Mausteet", "category": "Mausteet"}, '
            '"2": {"parent": "Mausteet", "category": "Mausteet"}}'
        )
        _ai_group_products(grocy, "gemini-key")
        # Product 1 should NOT get parent_id (min_stock > 0)
        # but SHOULD get product_group_id.
        calls = [str(c) for c in grocy.update_product.call_args_list]
        parent_calls_for_1 = [c for c in grocy.update_product.call_args_list
                              if c[0][0] == 1 and "parent_id" in (c[1] if len(c) > 1 else {})]
        assert len(parent_calls_for_1) == 0, f"Product 1 should not get parent: {calls}"
        # Product 1 should get product_group_id.
        grocy.update_product.assert_any_call(1, product_group_id=self._CATEGORY_GROUP_ID)
        # Product 2 gets parent normally.
        grocy.update_product.assert_any_call(2, parent_id=100, product_group_id=self._CATEGORY_GROUP_ID)

    # -- Incremental-mode tests (product_ids=[...]) ------------------------

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_incremental_mode_uses_dedup(self, mock_gemini, _mock_dedup):
        """Incremental mode: dedup IS called and only allowed IDs go to Gemini."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka maito"},
            {"id": 2, "name": "Valio maito"},
            {"id": 3, "name": "Ketsuppi"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maito"}}'
        )

        result = _ai_group_products(grocy, "gemini-key", product_ids=[1])
        assert result == 1
        # Dedup IS called in incremental mode.
        _mock_dedup.assert_called_once()
        # Only product 1 in the prompt.
        prompt = mock_gemini.call_args[0][0]
        assert "Pirkka maito" in prompt
        assert "Valio maito" not in prompt
        assert "Ketsuppi" not in prompt

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_incremental_mode_skips_already_grouped(self, mock_gemini, _mock_dedup):
        """Incremental mode: products with a parent are skipped (not stripped)."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka maito", "parent_id": 99},
            {"id": 2, "name": "Valio maito"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"2": {"parent": "Maito", "category": "Maitotaloustuotteet"}}'
        )

        result = _ai_group_products(grocy, "gemini-key", product_ids=[1, 2])
        assert result == 1
        # Only product 2 sent to Gemini (product 1 already has a parent).
        prompt_text = mock_gemini.call_args[0][0]
        assert "Valio maito" in prompt_text
        assert "Pirkka maito" not in prompt_text

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_incremental_mode_includes_existing_hints(self, mock_gemini, _mock_dedup):
        """Incremental mode: existing parent/category names appear in prompt."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [
            {"id": 10, "name": "Sipuli"},
            {"id": 11, "name": "Punasipuli", "parent_id": 10},
            {"id": 20, "name": "Juusto"},
        ]
        grocy = self._make_grocy(products)
        grocy.get_product_groups.return_value = [
            {"id": 1, "name": "Vihannekset"},
            {"id": 2, "name": "Group master"},
        ]
        mock_gemini.return_value = (
            '{"20": {"parent": "Juustot", "category": "Maitotaloustuotteet"}}'
        )

        _ai_group_products(grocy, "gemini-key", product_ids=[20])
        prompt_text = mock_gemini.call_args[0][0]
        assert "Existing parent products" in prompt_text
        assert '"Sipuli"' in prompt_text
        assert "Existing product categories" in prompt_text
        assert '"Vihannekset"' in prompt_text

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_uses_optimize_model(self, mock_gemini, _mock_dedup):
        """Full mode uses optimize_model when provided."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [{"id": 1, "name": "Pirkka maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maito"}}'
        )

        _ai_group_products(
            grocy, "gemini-key", "gemini-1.5-flash",
            optimize_model="gemini-2.0-pro",
        )
        # The Gemini call should use the optimize model in full mode.
        call_model = mock_gemini.call_args[0][2]
        assert call_model == "gemini-2.0-pro"

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_falls_back_to_regular_model(self, mock_gemini, _mock_dedup):
        """Full mode falls back to regular model when optimize_model is empty."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [{"id": 1, "name": "Pirkka maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maito"}}'
        )

        _ai_group_products(
            grocy, "gemini-key", "gemini-1.5-flash",
            optimize_model="",
        )
        call_model = mock_gemini.call_args[0][2]
        assert call_model == "gemini-1.5-flash"

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_incremental_mode_uses_regular_model(self, mock_gemini, _mock_dedup):
        """Incremental mode always uses the regular model, not optimize_model."""
        from grocy_scraper_addon.main import _ai_group_products
        products = [{"id": 1, "name": "Pirkka maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = (
            '{"1": {"parent": "Maito", "category": "Maito"}}'
        )

        _ai_group_products(
            grocy, "gemini-key", "gemini-1.5-flash",
            optimize_model="gemini-2.0-pro",
            product_ids=[1],
        )
        call_model = mock_gemini.call_args[0][2]
        assert call_model == "gemini-1.5-flash"


# ---------------------------------------------------------------------------
# main – AI mode integration
# ---------------------------------------------------------------------------

class TestMainAIMode:
    @patch("grocy_scraper_addon.main._ai_sort_products")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_sort_mode_calls_ai_sort(self, MockGrocy, mock_sort):
        mock_sort.return_value = 3
        rc = main([
            "--sort",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_sort.assert_called_once()

    @patch("grocy_scraper_addon.main._ai_assign_due_dates")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_date_mode_calls_ai_dates(self, MockGrocy, mock_date):
        mock_date.return_value = 5
        rc = main([
            "--date",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_date.assert_called_once()

    @patch("grocy_scraper_addon.main._ai_assign_due_dates")
    @patch("grocy_scraper_addon.main._ai_sort_products")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_sort_and_date_together(self, MockGrocy, mock_sort, mock_date):
        mock_sort.return_value = 2
        mock_date.return_value = 2
        rc = main([
            "--sort", "--date",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_sort.assert_called_once()
        mock_date.assert_called_once()

    def test_missing_gemini_key_returns_1(self):
        rc = main([
            "--sort",
            "--storage-url", "https://grocy.example.com",
        ])
        assert rc == 1

    @patch("grocy_scraper_addon.main._ai_group_products")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_group_mode_calls_ai_group(self, MockGrocy, mock_group):
        mock_group.return_value = 4
        rc = main([
            "--group",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_group.assert_called_once()


class TestDiscoverChainsAI:
    """Discover mode should run optimize when a Gemini key is set."""

    @patch("grocy_scraper_addon.main._ai_optimize_products")
    @patch("grocy_scraper_addon.main._discover_products", return_value=(0, [42, 99]))
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_discover_chains_optimize(
        self, MockGrocy, mock_discover, mock_optimize,
    ):
        mock_optimize.return_value = 3
        rc = main([
            "--discover",
            "--store", "N110",
            "--storage-url", "https://grocy.example.com",
            "--location-id", "2",
            "--quantity-unit-id", "2",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_discover.assert_called_once()
        mock_optimize.assert_called_once()
        _, opt_kwargs = mock_optimize.call_args
        assert opt_kwargs["product_ids"] == [42, 99]

    @patch("grocy_scraper_addon.main._ai_optimize_products")
    @patch("grocy_scraper_addon.main._discover_products", return_value=(0, [42]))
    def test_discover_no_gemini_key_skips_ai(
        self, mock_discover, mock_optimize,
    ):
        rc = main([
            "--discover",
            "--store", "N110",
            "--storage-url", "https://grocy.example.com",
            "--location-id", "2",
            "--quantity-unit-id", "2",
            "--gemini-api-key", "",
        ])
        assert rc == 0
        mock_discover.assert_called_once()
        mock_optimize.assert_not_called()

    @patch("grocy_scraper_addon.main._ai_optimize_products")
    @patch("grocy_scraper_addon.main._discover_products", return_value=(1, []))
    def test_discover_failure_skips_ai(
        self, mock_discover, mock_optimize,
    ):
        rc = main([
            "--discover",
            "--store", "N110",
            "--storage-url", "https://grocy.example.com",
            "--location-id", "2",
            "--quantity-unit-id", "2",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 1
        mock_discover.assert_called_once()
        mock_optimize.assert_not_called()

    @patch("grocy_scraper_addon.main._ai_optimize_products")
    @patch("grocy_scraper_addon.main._discover_products", return_value=(0, []))
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_discover_no_new_products_skips_ai(
        self, MockGrocy, mock_discover, mock_optimize,
    ):
        """When discover succeeds but finds no new products, AI is skipped."""
        rc = main([
            "--discover",
            "--store", "N110",
            "--storage-url", "https://grocy.example.com",
            "--location-id", "2",
            "--quantity-unit-id", "2",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_discover.assert_called_once()
        mock_optimize.assert_not_called()

class TestParseArgsDeleteAll:
    def test_delete_all_flag(self):
        args = parse_args([
            "--delete-all",
            "--storage-url", "https://grocy.example.com",
        ])
        assert args.delete_all is True

    def test_delete_all_mutually_exclusive_with_query(self):
        with pytest.raises(SystemExit):
            parse_args(["--delete-all", "--query", "maito"])

    def test_delete_all_mutually_exclusive_with_browse(self):
        with pytest.raises(SystemExit):
            parse_args(["--delete-all", "--browse"])

    def test_delete_all_mutually_exclusive_with_discover(self):
        with pytest.raises(SystemExit):
            parse_args(["--delete-all", "--discover"])


# ---------------------------------------------------------------------------
# _validate_args – --delete-all
# ---------------------------------------------------------------------------

class TestValidateArgsDeleteAll:
    def _base_args(self, **overrides):
        from argparse import Namespace
        defaults = dict(
            delete_all=True,
            storage_url="https://grocy.example.com",
            query=None, browse=False, discover=False,
            sort=False, date=False, group=False, optimize=False,
            update=False,
            dry_run=False,
            store="",
            location_id=None,
            quantity_unit_id=None,
            gemini_api_key="",
        )
        defaults.update(overrides)
        return Namespace(**defaults)

    def test_valid_delete_all_passes(self):
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_args()) == 0

    def test_missing_storage_url_fails(self):
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_args(storage_url="")) == 1

    def test_store_not_required(self):
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_args(store="")) == 0


# ---------------------------------------------------------------------------
# _delete_all_products
# ---------------------------------------------------------------------------

class TestDeleteAllProducts:
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_empty_database_returns_0(self, MockGrocy):
        from grocy_scraper_addon.main import _delete_all_products
        grocy = MockGrocy.return_value
        grocy.get_all_products.return_value = []
        assert _delete_all_products(grocy) == 0
        grocy.delete_product.assert_not_called()

    @patch("grocy_scraper_addon.main.StorageClient")
    def test_deletes_all_products(self, MockGrocy):
        from grocy_scraper_addon.main import _delete_all_products
        grocy = MockGrocy.return_value
        grocy.get_all_products.return_value = [
            {"id": 1, "name": "Milk"},
            {"id": 2, "name": "Bread"},
            {"id": 3, "name": "Eggs"},
        ]
        assert _delete_all_products(grocy) == 0
        assert grocy.delete_product.call_count == 3
        grocy.delete_product.assert_any_call(1)
        grocy.delete_product.assert_any_call(2)
        grocy.delete_product.assert_any_call(3)

    @patch("grocy_scraper_addon.main.StorageClient")
    def test_fetch_error_returns_1(self, MockGrocy):
        from grocy_scraper_addon.main import _delete_all_products
        grocy = MockGrocy.return_value
        grocy.get_all_products.side_effect = StorageAPIError("connection refused")
        assert _delete_all_products(grocy) == 1

    @patch("grocy_scraper_addon.main.StorageClient")
    def test_partial_failure_returns_1(self, MockGrocy):
        from grocy_scraper_addon.main import _delete_all_products
        grocy = MockGrocy.return_value
        grocy.get_all_products.return_value = [
            {"id": 1, "name": "Milk"},
            {"id": 2, "name": "Bread"},
        ]
        grocy.delete_product.side_effect = [None, StorageAPIError("failed")]
        assert _delete_all_products(grocy) == 1
        assert grocy.delete_product.call_count == 2


# ---------------------------------------------------------------------------
# parse_args – --discover flag
# ---------------------------------------------------------------------------

class TestParseArgsDiscover:
    def test_discover_flag(self):
        args = parse_args([
            "--discover",
            "--store", "N110",
            "--storage-url", "https://grocy.example.com",
            "--location-id", "2",
            "--quantity-unit-id", "2",
        ])
        assert args.discover is True

    def test_discover_mutually_exclusive_with_query(self):
        with pytest.raises(SystemExit):
            parse_args(["--discover", "--query", "maito"])

    def test_discover_mutually_exclusive_with_browse(self):
        with pytest.raises(SystemExit):
            parse_args(["--discover", "--browse"])


# ---------------------------------------------------------------------------
# _validate_args – --discover mode
# ---------------------------------------------------------------------------

class TestValidateArgsDiscover:
    def _base_discover_args(self, **overrides):
        from argparse import Namespace
        defaults = dict(
            discover=True, query=None, browse=False,
            sort=False, date=False, group=False, optimize=False,
            delete_all=False, update=False,
            store="N110",
            storage_url="https://grocy.example.com",
            location_id=2,
            quantity_unit_id=2,
            dry_run=False,
            gemini_api_key="",
        )
        defaults.update(overrides)
        return Namespace(**defaults)

    def test_valid_discover_passes(self):
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_discover_args()) == 0

    def test_missing_store_fails(self):
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_discover_args(store="")) == 1

    def test_missing_storage_url_fails(self):
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_discover_args(storage_url="")) == 1

    def test_missing_location_id_ok(self):
        """location_id=None is now allowed (auto-detected by StorageClient)."""
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_discover_args(location_id=None)) == 0

    def test_missing_quantity_unit_id_ok(self):
        """quantity_unit_id=None is now allowed (auto-detected by StorageClient)."""
        from grocy_scraper_addon.main import _validate_args
        assert _validate_args(self._base_discover_args(quantity_unit_id=None)) == 0


# ---------------------------------------------------------------------------
# _discover_products
# ---------------------------------------------------------------------------

class TestDiscoverProducts:
    @patch("grocy_scraper_addon.main.KRuokaScraper")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_no_pending_returns_0(self, MockGrocy, MockScraper):
        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
        grocy_instance.get_barcode_queue.return_value = []

        from grocy_scraper_addon.main import _discover_products
        from argparse import Namespace
        args = Namespace(
            storage_url="https://grocy.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 0
        assert discovered_ids == []

    @patch("grocy_scraper_addon.main.KRuokaScraper")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_unknown_barcode_searched_on_kruoka(self, MockGrocy, MockScraper):
        from grocy_scraper_addon.main import _discover_products
        from argparse import Namespace

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
        grocy_instance.get_barcode_queue.return_value = [
            {"id": 42, "barcode": "6410405082657", "source": "scanner"},
        ]
        grocy_instance.get_product_by_barcode.side_effect = [
            None,  # sync_product check
            {"id": 99, "name": "Maito"},  # post-creation lookup
        ]
        grocy_instance.create_product.return_value = 99

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([
            Product(name="Pirkka kevytmaito 1l", ean="6410405082657"),
        ])

        args = Namespace(
            storage_url="https://grocy.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 0
        assert discovered_ids == [99]
        scraper_instance.search.assert_called_once()
        grocy_instance.create_product.assert_called_once()
        grocy_instance.add_stock.assert_called_once_with(99, amount=1.0)
        grocy_instance.update_barcode_queue_item.assert_called_once_with(
            42, status="done", result_product_id=99,
        )

    @patch("grocy_scraper_addon.main.skaupat_lookup")
    @patch("grocy_scraper_addon.main.KRuokaScraper")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_not_found_on_kruoka_or_skaupat_skips(self, MockGrocy, MockScraper, mock_sk):
        from grocy_scraper_addon.main import _discover_products
        from argparse import Namespace

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
        grocy_instance.get_barcode_queue.return_value = [
            {"id": 42, "barcode": "0000000000000", "source": "scanner"},
        ]

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([])
        mock_sk.return_value = None

        args = Namespace(
            storage_url="https://grocy.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 0
        assert discovered_ids == []
        grocy_instance.create_product.assert_not_called()
        grocy_instance.update_barcode_queue_item.assert_called_once_with(
            42, status="error",
            error_message="Product not found for EAN 0000000000000",
        )
        mock_sk.assert_called_once_with("0000000000000")

    @patch("grocy_scraper_addon.main.skaupat_lookup")
    @patch("grocy_scraper_addon.main.KRuokaScraper")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_skaupat_fallback_creates_product(self, MockGrocy, MockScraper, mock_sk):
        """When K-Ruoka has no match, S-kaupat result is used."""
        from grocy_scraper.skaupat_client import SKaupatProduct
        from grocy_scraper_addon.main import _discover_products
        from argparse import Namespace

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
        grocy_instance.get_barcode_queue.return_value = [
            {"id": 77, "barcode": "6414893095588", "source": "scanner"},
        ]
        grocy_instance.get_product_by_barcode.side_effect = [
            None,
            {"id": 88, "name": "Kotimaista luomukananmunat M6"},
        ]
        grocy_instance.create_product.return_value = 88

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([])  # K-Ruoka empty.

        mock_sk.return_value = SKaupatProduct(
            name="Kotimaista luomukananmunat M6",
            ean="6414893095588",
            description="Luomumunia 6 kpl.",
            brand="Kotimaista",
            image_url="https://cdn.s-cloud.fi/v1/w720h720@_q75/product/ean/6414893095588_kuva1.webp",
        )

        args = Namespace(
            storage_url="https://grocy.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 0
        assert discovered_ids == [88]
        mock_sk.assert_called_once_with("6414893095588")
        grocy_instance.create_product.assert_called_once()
        call_args = grocy_instance.create_product.call_args
        assert call_args[1]["name"] == "Kotimaista luomukananmunat M6"
        grocy_instance.add_stock.assert_called_once_with(88, amount=1.0)
        grocy_instance.update_barcode_queue_item.assert_called_once_with(
            77, status="done", result_product_id=88,
        )

    @patch("grocy_scraper_addon.main.KRuokaScraper")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_queue_fetch_error_returns_1(self, MockGrocy, MockScraper):
        from grocy_scraper.storage_client import StorageAPIError
        from grocy_scraper_addon.main import _discover_products
        from argparse import Namespace

        MockGrocy.return_value.get_all_barcodes.return_value = []
        MockGrocy.return_value.get_barcode_queue.side_effect = StorageAPIError("fail")

        args = Namespace(
            storage_url="https://grocy.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 1
        assert discovered_ids == []


# ---------------------------------------------------------------------------
# _parse_store_ids
# ---------------------------------------------------------------------------

class TestParseStoreIds:
    def test_single_store(self):
        assert _parse_store_ids("N110") == ["N110"]

    def test_multiple_stores(self):
        assert _parse_store_ids("N110,N137") == ["N110", "N137"]

    def test_strips_whitespace(self):
        assert _parse_store_ids(" N110 , N137 ") == ["N110", "N137"]

    def test_empty_string(self):
        assert _parse_store_ids("") == []

    def test_trailing_comma(self):
        assert _parse_store_ids("N110,") == ["N110"]

    def test_only_commas(self):
        assert _parse_store_ids(",,,") == []


# ---------------------------------------------------------------------------
# Multi-store fallback – _run_scraper
# ---------------------------------------------------------------------------

class TestMultiStoreFallbackRunScraper:
    """Test that _run_scraper tries the next store when one fails."""

    def test_first_store_succeeds(self, capsys):
        """When the first store works, only one scraper is created."""
        products = [Product(name="Maito", ean="111")]
        with patch("grocy_scraper_addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter(products)
            rc = main(["--store", "N110,N137", "--browse", "--dry-run"])

        assert rc == 0
        # Only one scraper should be instantiated (first store succeeded).
        assert MockScraper.call_count == 1
        _, kwargs = MockScraper.call_args
        assert kwargs["store_id"] == "N110"

    def test_fallback_to_second_store_on_error(self, capsys):
        """When the first store raises, the second store is tried."""
        products = [Product(name="Maito", ean="111")]
        call_count = {"n": 0}

        def make_scraper(**kwargs):
            call_count["n"] += 1
            instance = MagicMock()
            if call_count["n"] == 1:
                # First store fails.
                instance.browse.return_value = iter([])
                instance.browse.side_effect = RuntimeError("store down")
            else:
                # Second store succeeds.
                instance.browse.return_value = iter(products)
            return instance

        with patch("grocy_scraper_addon.main.KRuokaScraper", side_effect=make_scraper):
            rc = main(["--store", "N110,N137", "--browse", "--dry-run"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Maito" in out

    def test_all_stores_fail_raises(self, capsys):
        """When all stores fail, the last error is raised."""
        with patch("grocy_scraper_addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.side_effect = RuntimeError("all fail")
            with pytest.raises(RuntimeError, match="all fail"):
                main(["--store", "N110,N137", "--browse", "--dry-run"])

    def test_single_store_still_works(self, capsys):
        """Single store (no comma) still works as before."""
        products = [Product(name="Kerma", ean="222")]
        with patch("grocy_scraper_addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.search.return_value = iter(products)
            rc = main(["--store", "N110", "--query", "kerma", "--dry-run"])

        assert rc == 0
        _, kwargs = MockScraper.call_args
        assert kwargs["store_id"] == "N110"


# ---------------------------------------------------------------------------
# Multi-store fallback – _discover_products
# ---------------------------------------------------------------------------

class TestMultiStoreDiscoverFallback:
    """Test that discover tries multiple stores for each barcode."""

    @patch("grocy_scraper_addon.main.skaupat_lookup", return_value=None)
    @patch("grocy_scraper_addon.main.StorageClient")
    @patch("grocy_scraper_addon.main.KRuokaScraper")
    def test_discover_tries_second_store(
        self, MockScraper, MockGrocy, mock_skaupat,
    ):
        from grocy_scraper_addon.main import _discover_products

        # Set up barcode queue to return one pending barcode.
        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
        grocy_instance.get_barcode_queue.return_value = [
            {"id": 10, "barcode": "123", "source": "scanner"},
        ]
        grocy_instance.get_product_by_barcode.return_value = {"id": 1}

        # First scraper (store N110) raises; second scraper (N137) succeeds.
        scrapers = []
        call_idx = {"n": 0}

        def make_scraper(**kwargs):
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                s.search.side_effect = RuntimeError("store N110 down")
            else:
                s.search.return_value = iter([
                    Product(name="Milk", ean="123"),
                ])
            scrapers.append(s)
            return s

        MockScraper.side_effect = make_scraper

        args = Namespace(
            discover=True, query=None, browse=False,
            sort=False, date=False, group=False, optimize=False,
            delete_all=False, update=False,
            storage_url="https://grocy.example.com",
            store="N110,N137",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
            skip_existing=False,
        )
        rc, discovered_ids = _discover_products(args)
        # Two scrapers created for the two stores.
        assert MockScraper.call_count == 2
        # The product was found via the second store and synced.
        assert rc == 0
        assert discovered_ids == [1]


# ---------------------------------------------------------------------------
# Multi-store fallback – _update_products
# ---------------------------------------------------------------------------

class TestMultiStoreUpdateFallback:
    """Test that update tries multiple stores for each barcode."""

    @patch("grocy_scraper_addon.main.skaupat_lookup", return_value=None)
    @patch("grocy_scraper_addon.main.StorageClient")
    @patch("grocy_scraper_addon.main.KRuokaScraper")
    def test_update_tries_second_store(
        self, MockScraper, MockGrocy, mock_skaupat,
    ):
        from grocy_scraper_addon.main import _update_products

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_products.return_value = [
            {"id": 1, "name": "Old Milk"},
        ]
        grocy_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "123"},
        ]

        # First scraper (store N110) raises; second (N137) succeeds.
        call_idx = {"n": 0}

        def make_scraper(**kwargs):
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                s.search.side_effect = RuntimeError("store N110 down")
            else:
                s.search.return_value = iter([
                    Product(name="New Milk", ean="123"),
                ])
            return s

        MockScraper.side_effect = make_scraper

        args = Namespace(
            store="N110,N137",
            storage_url="https://grocy.example.com",
            use_graphql=True,
            upload_images=False,
            max_products=None,
        )
        rc = _update_products(args)
        assert MockScraper.call_count == 2
        assert rc == 0
        grocy_instance.update_product.assert_called_once()


# ---------------------------------------------------------------------------
# --optimize flag
# ---------------------------------------------------------------------------

class TestParseArgsOptimize:
    def test_optimize_flag(self):
        args = parse_args([
            "--optimize",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.optimize is True

    def test_optimize_default_false(self):
        args = parse_args(["--store", "N110", "--browse", "--dry-run"])
        assert args.optimize is False


class TestValidateArgsOptimize:
    def test_valid_optimize_passes(self):
        from grocy_scraper_addon.main import _validate_args
        args = Namespace(
            sort=False, date=False, group=False, optimize=True,
            storage_url="https://grocy.example.com",
            gemini_api_key="GEMINI_KEY",
            query=None, browse=False,
            discover=False, delete_all=False, update=False,
            dry_run=False,
            store="", location_id=None, quantity_unit_id=None,
        )
        assert _validate_args(args) == 0

    def test_optimize_missing_gemini_key_fails(self):
        from grocy_scraper_addon.main import _validate_args
        args = Namespace(
            sort=False, date=False, group=False, optimize=True,
            storage_url="https://grocy.example.com",
            gemini_api_key="",
            query=None, browse=False,
            discover=False, delete_all=False, update=False,
            dry_run=False,
            store="", location_id=None, quantity_unit_id=None,
        )
        assert _validate_args(args) == 1


class TestMainOptimizeMode:
    @patch("grocy_scraper_addon.main._ai_optimize_products")
    @patch("grocy_scraper_addon.main.StorageClient")
    def test_optimize_mode_calls_ai_optimize(self, MockGrocy, mock_optimize):
        mock_optimize.return_value = 5
        rc = main([
            "--optimize",
            "--storage-url", "https://grocy.example.com",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_optimize.assert_called_once()


@patch("grocy_scraper_addon.main._fix_broken_product_units", return_value=0)
@patch("grocy_scraper_addon.main._ai_detect_package_sizes", return_value=0)
@patch("grocy_scraper_addon.main._ensure_units_and_conversions", return_value={"piece": 1})
@patch("grocy_scraper_addon.main._optimize_units", return_value=0)
@patch("grocy_scraper_addon.main._deduplicate_parent_products", return_value=(0, {}))
class TestAiOptimizeProducts:
    def _make_grocy(self, products, locations):
        g = MagicMock(spec=StorageClient)
        g.get_all_products.return_value = products
        g.get_locations.return_value = locations
        g.update_product.return_value = None
        g.get_product_stock_locations.return_value = []
        g.transfer_stock.return_value = None
        g.ensure_product_group.return_value = 100
        g.create_product.return_value = 999
        g.get_product_barcodes.return_value = []
        g.update_barcode.return_value = None
        g.delete_product.return_value = None
        g.delete_product_image.return_value = None
        g.get_product_groups.return_value = []
        g.delete_product_group.return_value = None
        g.get_quantity_units.return_value = []
        g.get_quantity_unit_conversions.return_value = []
        g.create_quantity_unit_conversion.return_value = None
        return g

    # -- Full-mode tests (product_ids=None, clean-slate) -------------------

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_sort_date_group_in_single_pass(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: sort, date, and group in one Gemini call."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 1, "name": "Maito 1L"},
            {"id": 2, "name": "Pesuaine"},
        ]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 3, "name": "Cabinet"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 14, '
            '"group_name": "Maito", "category": "Maitotaloustuotteet", '
            '"pack_size": null, "pack_unit": null}, '
            '"2": {"location_id": 3, "best_before_days": 1095, '
            '"group_name": null, "category": "Siivous", '
            '"pack_size": null, "pack_unit": null}}'
        )

        result = _ai_optimize_products(grocy, "gemini-key")
        # Dedup NOT called in full mode.
        _mock_dedup.assert_not_called()
        # Full _optimize_units IS called in full mode.
        _mock_opt.assert_called_once()
        # Incremental unit functions are NOT called in full mode.
        _mock_ens.assert_not_called()
        _mock_pkg.assert_not_called()
        # 2 location + 2 date + 1 group = 5 updates minimum
        assert result >= 5
        # Check sort (location)
        grocy.update_product.assert_any_call(1, location_id=2)
        grocy.update_product.assert_any_call(2, location_id=3)
        # Check date (best_before_days)
        grocy.update_product.assert_any_call(1, default_best_before_days=14)
        grocy.update_product.assert_any_call(2, default_best_before_days=1095)
        # Check group: product 1 should be grouped under the new parent
        grocy.create_product.assert_called()
        grocy.update_product.assert_any_call(
            1, parent_id=999, product_group_id=100,
        )

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_pack_detection_sets_barcode_pack_info(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 1, "name": "Red Bull"},
            {"id": 2, "name": "Red Bull 4-pack"},
        ]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_quantity_units.return_value = [
            {"id": 5, "name": "Piece", "description": "kpl"},
        ]
        grocy.get_product_barcodes.return_value = [
            {"id": 10, "barcode": "1234567890123", "product_id": 2, "amount": 1},
        ]
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 365, '
            '"group_name": null, "category": "Juomat", '
            '"pack_size": null, "pack_unit": null}, '
            '"2": {"location_id": 2, "best_before_days": 365, '
            '"group_name": null, "category": "Juomat", '
            '"pack_size": 4, "pack_unit": "kpl"}}'
        )

        result = _ai_optimize_products(grocy, "gemini-key")
        assert result >= 1
        # Barcode updated with pack_size and pack_unit_id
        grocy.update_barcode.assert_any_call(10, pack_size=4, pack_unit_id=5)
        # Product is NOT deleted (stays as-is)
        grocy.delete_product.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_no_products_returns_zero(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        from grocy_scraper_addon.main import _ai_optimize_products
        grocy = self._make_grocy([], [])
        result = _ai_optimize_products(grocy, "gemini-key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_gemini_failure_continues(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.side_effect = StorageAPIError("API down")

        result = _ai_optimize_products(grocy, "gemini-key")
        assert result == 0

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_strips_parents_and_sends_all(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: strips parent assignments and sends all leaf products."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 10, "name": "Mausteet",
             "active": False},
            {"id": 11, "name": "Mustapippuri", "parent_id": 10},
            {"id": 12, "name": "Oregano", "parent_id": 10},
        ]
        locations = [{"id": 2, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_all_products.side_effect = [
            products,
            # Old parent cleanup: 10 still has children -> keep.
            [
                {"id": 10, "name": "Mausteet",
                 "active": False},
                {"id": 11, "name": "Mustapippuri", "parent_id": 10},
                {"id": 12, "name": "Oregano", "parent_id": 10},
            ],
            # PG cleanup.
            [
                {"id": 11, "name": "Mustapippuri", "product_group_id": 100},
                {"id": 12, "name": "Oregano", "product_group_id": 100},
            ],
        ]
        mock_gemini.return_value = (
            '{"11": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}, '
            '"12": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}}'
        )

        _ai_optimize_products(grocy, "gemini-key")
        # Parents should be stripped.
        grocy.update_product.assert_any_call(11, parent_id="")
        grocy.update_product.assert_any_call(12, parent_id="")
        # Old parent placeholder (10) should NOT be in the Gemini prompt.
        prompt = mock_gemini.call_args[0][0]
        assert "Mausteet" not in prompt.split("Products:")[-1]

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_deletes_old_parent_placeholders(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: old parent placeholders with no new children are deleted."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 20, "name": "Mauste",
             "active": False},
            {"id": 11, "name": "Mustapippuri", "parent_id": 20},
            {"id": 12, "name": "Oregano", "parent_id": 20},
        ]
        locations = [{"id": 2, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_all_products.side_effect = [
            products,
            # After rebuild: 20 has no children (reassigned to 999).
            [
                {"id": 20, "name": "Mauste",
                 "active": False},
                {"id": 11, "name": "Mustapippuri", "parent_id": 999},
                {"id": 12, "name": "Oregano", "parent_id": 999},
                {"id": 999, "name": "Mausteet"},
            ],
            # PG cleanup.
            [
                {"id": 11, "name": "Mustapippuri", "product_group_id": 100},
                {"id": 12, "name": "Oregano", "product_group_id": 100},
            ],
        ]
        mock_gemini.return_value = (
            '{"11": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}, '
            '"12": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}}'
        )

        result = _ai_optimize_products(grocy, "gemini-key")
        grocy.delete_product.assert_any_call(20)
        assert result >= 1

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_cleans_unused_product_groups(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: unused product groups are deleted after rebuild."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 14, '
            '"group_name": null, "category": "Maitotaloustuotteet", '
            '"pack_size": null, "pack_unit": null}}'
        )
        grocy.get_all_products.side_effect = [
            products,
            # PG cleanup: product uses group 100, but group 70 is unused.
            [{"id": 1, "name": "Maito", "product_group_id": 100}],
        ]
        grocy.get_product_groups.return_value = [
            {"id": 100, "name": "Maitotaloustuotteet"},
            {"id": 70, "name": "Vanhat"},
            {"id": 50, "name": "Group master"},
        ]

        _ai_optimize_products(grocy, "gemini-key")
        grocy.delete_product_group.assert_called_once_with(70)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_no_existing_hints_in_prompt(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: prompt does NOT include existing parent/category hints."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 10, "name": "Sipuli"},
            {"id": 11, "name": "Punasipuli", "parent_id": 10},
        ]
        locations = [{"id": 2, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"10": {"location_id": 2, "best_before_days": 14, '
            '"group_name": null, "category": "Vihannekset", '
            '"pack_size": null, "pack_unit": null}, '
            '"11": {"location_id": 2, "best_before_days": 14, '
            '"group_name": "Sipuli", "category": "Vihannekset", '
            '"pack_size": null, "pack_unit": null}}'
        )

        _ai_optimize_products(grocy, "gemini-key")
        prompt = mock_gemini.call_args[0][0]
        assert "Existing parent products" not in prompt
        assert "Existing product categories" not in prompt

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_skips_parent_for_min_stock_product(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: products with min_stock_amount > 0 skip parent assignment."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 1, "name": "Mustapippuri 100g", "min_stock_amount": 1},
            {"id": 2, "name": "Oregano 50g"},
        ]
        locations = [{"id": 2, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}, '
            '"2": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}}'
        )
        _ai_optimize_products(grocy, "gemini-key")
        # Product 1 should NOT get parent_id (min_stock > 0)
        calls = grocy.update_product.call_args_list
        parent_calls_for_1 = [c for c in calls
                              if c[0][0] == 1 and "parent_id" in (c[1] if len(c) > 1 else {})]
        assert len(parent_calls_for_1) == 0, f"Product 1 should not get parent: {calls}"
        # Product 1 SHOULD still get product_group_id.
        grocy.update_product.assert_any_call(1, product_group_id=100)
        # Product 2 gets parent normally.
        grocy.update_product.assert_any_call(
            2, parent_id=999, product_group_id=100,
        )

    # -- Incremental-mode tests (product_ids=[...]) ------------------------

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_product_ids_filter(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 1, "name": "Maito"},
            {"id": 2, "name": "Leip\u00e4"},
            {"id": 3, "name": "Ketsuppi"},
        ]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 14, '
            '"group_name": null, "category": "Maito", '
            '"pack_size": null, "pack_unit": null}}'
        )

        result = _ai_optimize_products(grocy, "gemini-key", product_ids=[1])
        assert result >= 1
        # Dedup is NOT called in incremental mode (too heavy for single scan).
        _mock_dedup.assert_not_called()
        # Full _optimize_units is NOT called in incremental mode.
        _mock_opt.assert_not_called()
        # But lightweight unit ensure + package detection ARE called.
        _mock_ens.assert_called_once()
        _mock_pkg.assert_called_once()
        # Package detection receives only the filtered product(s).
        pkg_products = _mock_pkg.call_args[0][1]
        assert len(pkg_products) == 1
        assert pkg_products[0]["name"] == "Maito"
        # Only product 1 should be in the prompt
        prompt = mock_gemini.call_args[0][0]
        assert "Maito" in prompt
        assert "Leip\u00e4" not in prompt
        assert "Ketsuppi" not in prompt

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_incremental_existing_parents_in_prompt(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Incremental mode: existing parent/category names appear in prompt."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 10, "name": "Mausteet"},
            {"id": 11, "name": "Mustapippuri", "parent_id": 10},
            {"id": 12, "name": "Timjami"},
        ]
        locations = [{"id": 2, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_product_groups.return_value = [
            {"id": 1, "name": "Mausteet"},
            {"id": 2, "name": "Group master"},
        ]
        mock_gemini.return_value = (
            '{"12": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}}'
        )

        _ai_optimize_products(grocy, "gemini-key", product_ids=[12])
        prompt = mock_gemini.call_args[0][0]
        # Only product 12 in the Products section
        assert "Timjami" in prompt
        assert "Mustapippuri" not in prompt.split("Products:")[-1]
        # Existing parent and category hints are in the prompt header
        assert "Existing parent products" in prompt
        assert '"Mausteet"' in prompt
        assert "Existing product categories" in prompt

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_regroup_product_under_different_parent(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: product re-grouped from old parent to new one."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 10, "name": "Mausteet"},
            {"id": 20, "name": "Mauste",
             "active": False},
            {"id": 11, "name": "Mustapippuri", "parent_id": 20},
            {"id": 12, "name": "Oregano", "parent_id": 20},
        ]
        locations = [{"id": 2, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_all_products.side_effect = [
            products,
            # After rebuild: 20 has no children.
            [
                {"id": 10, "name": "Mausteet"},
                {"id": 20, "name": "Mauste",
                 "active": False},
                {"id": 11, "name": "Mustapippuri", "parent_id": 10},
                {"id": 12, "name": "Oregano", "parent_id": 10},
            ],
            # PG cleanup.
            [
                {"id": 11, "name": "Mustapippuri", "product_group_id": 100},
                {"id": 12, "name": "Oregano", "product_group_id": 100},
            ],
        ]
        # Gemini sees 10, 11, 12 (20 is old parent placeholder, filtered out).
        mock_gemini.return_value = (
            '{"10": {"location_id": 2, "best_before_days": 730, '
            '"group_name": null, "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}, '
            '"11": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}, '
            '"12": {"location_id": 2, "best_before_days": 730, '
            '"group_name": "Mausteet", "category": "Mausteet", '
            '"pack_size": null, "pack_unit": null}}'
        )

        result = _ai_optimize_products(grocy, "gemini-key")
        grocy.update_product.assert_any_call(
            11, parent_id=10, product_group_id=100,
        )
        grocy.update_product.assert_any_call(
            12, parent_id=10, product_group_id=100,
        )
        grocy.delete_product.assert_any_call(20)
        assert result >= 1

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_cleanup_empty_parent_after_optimize(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode: empty parent products are deleted after optimization."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 50, "name": "OldParent",
             "active": False},
            {"id": 51, "name": "Child A", "parent_id": 50},
        ]
        locations = [{"id": 2, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_all_products.side_effect = [
            products,
            # After rebuild: OldParent has no children.
            [
                {"id": 50, "name": "OldParent",
                 "active": False},
                {"id": 51, "name": "Child A", "parent_id": 999},
                {"id": 999, "name": "NewParent"},
            ],
            # PG cleanup.
            [{"id": 51, "name": "Child A", "product_group_id": 100}],
        ]
        mock_gemini.return_value = (
            '{"51": {"location_id": 2, "best_before_days": 365, '
            '"group_name": "NewParent", "category": "Muut", '
            '"pack_size": null, "pack_unit": null}}'
        )

        _ai_optimize_products(grocy, "gemini-key")
        grocy.delete_product.assert_any_call(50)

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_uses_optimize_model(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode uses optimize_model when provided."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 14, '
            '"group_name": null, "category": "Maito", '
            '"pack_size": null, "pack_unit": null}}'
        )

        _ai_optimize_products(
            grocy, "gemini-key", "gemini-1.5-flash",
            optimize_model="gemini-2.0-pro",
        )
        call_model = mock_gemini.call_args[0][2]
        assert call_model == "gemini-2.0-pro"

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_full_mode_falls_back_to_regular_model(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Full mode falls back to regular model when optimize_model is empty."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 14, '
            '"group_name": null, "category": "Maito", '
            '"pack_size": null, "pack_unit": null}}'
        )

        _ai_optimize_products(
            grocy, "gemini-key", "gemini-1.5-flash",
            optimize_model="",
        )
        call_model = mock_gemini.call_args[0][2]
        assert call_model == "gemini-1.5-flash"

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_incremental_mode_uses_regular_model(self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken):
        """Incremental mode always uses the regular model, not optimize_model."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = (
            '{"1": {"location_id": 2, "best_before_days": 14, '
            '"group_name": null, "category": "Maito", '
            '"pack_size": null, "pack_unit": null}}'
        )

        _ai_optimize_products(
            grocy, "gemini-key", "gemini-1.5-flash",
            optimize_model="gemini-2.0-pro",
            product_ids=[1],
        )
        call_model = mock_gemini.call_args[0][2]
        assert call_model == "gemini-1.5-flash"

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_incremental_pack_sets_barcode_pack_info(
        self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken,
    ):
        """Incremental mode: pack detection sets pack_size/pack_unit_id on barcode."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 2, "name": "Red Bull 4-pack"},
        ]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_quantity_units.return_value = [
            {"id": 5, "name": "Piece", "description": "kpl"},
        ]
        grocy.get_product_barcodes.return_value = [
            {"id": 10, "barcode": "1234567890123", "product_id": 2, "amount": 1},
        ]
        mock_gemini.return_value = (
            '{"2": {"location_id": 2, "best_before_days": 365, '
            '"group_name": null, "category": "Juomat", '
            '"pack_size": 4, "pack_unit": "kpl"}}'
        )

        _ai_optimize_products(grocy, "gemini-key", product_ids=[2])
        # Pack info set on barcode
        grocy.update_barcode.assert_any_call(10, pack_size=4, pack_unit_id=5)
        # Product NOT deleted — stays as-is
        grocy.delete_product.assert_not_called()
        # Unit optimization targets the product itself
        _mock_pkg.assert_called_once()
        pkg_products = _mock_pkg.call_args[0][1]
        ids_seen = {int(p["id"]) for p in pkg_products}
        assert 2 in ids_seen

    @patch("grocy_scraper_addon.main._call_gemini")
    def test_pack_handling_still_applies_sort_date_group(
        self, mock_gemini, _mock_dedup, _mock_opt, _mock_ens, _mock_pkg, _mock_fix_broken,
    ):
        """Pack products also get sort/date/group applied (product stays)."""
        from grocy_scraper_addon.main import _ai_optimize_products
        products = [
            {"id": 2, "name": "Pirkka vapaan kanan munia 10 kpl / 580g"},
        ]
        locations = [{"id": 3, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        grocy.get_quantity_units.return_value = [
            {"id": 5, "name": "Piece", "description": "kpl"},
        ]
        grocy.get_product_barcodes.return_value = [
            {"id": 10, "barcode": "6410402016242", "product_id": 2, "amount": 1},
        ]
        mock_gemini.return_value = (
            '{"2": {"location_id": 3, "best_before_days": 28, '
            '"group_name": "Kananmuna", "category": "Kananmunat", '
            '"pack_size": 10, "pack_unit": "kpl"}}'
        )

        _ai_optimize_products(grocy, "gemini-key", product_ids=[2])
        # Pack info set on barcode
        grocy.update_barcode.assert_any_call(10, pack_size=10, pack_unit_id=5)
        # Sort/date/group still applied to the SAME product
        grocy.update_product.assert_any_call(2, location_id=3)
        grocy.update_product.assert_any_call(2, default_best_before_days=28)


class TestCanonicalUnit:
    def test_standard_abbreviations(self):
        from grocy_scraper_addon.main import _canonical_unit
        assert _canonical_unit("g") == "g"
        assert _canonical_unit("kg") == "kg"
        assert _canonical_unit("ml") == "ml"
        assert _canonical_unit("dl") == "dl"
        assert _canonical_unit("l") == "l"

    def test_finnish_names(self):
        from grocy_scraper_addon.main import _canonical_unit
        assert _canonical_unit("gramma") == "g"
        assert _canonical_unit("kilogramma") == "kg"
        assert _canonical_unit("litra") == "l"
        assert _canonical_unit("desilitra") == "dl"

    def test_case_insensitive(self):
        from grocy_scraper_addon.main import _canonical_unit
        assert _canonical_unit("G") == "g"
        assert _canonical_unit("KG") == "kg"
        assert _canonical_unit("Litra") == "l"

    def test_unknown_returns_none(self):
        from grocy_scraper_addon.main import _canonical_unit
        assert _canonical_unit("unknown") is None
        assert _canonical_unit("") is None

    def test_piece_aliases(self):
        from grocy_scraper_addon.main import _canonical_unit
        assert _canonical_unit("kpl") == "kpl"
        assert _canonical_unit("pcs") == "kpl"
        assert _canonical_unit("piece") == "kpl"
        assert _canonical_unit("st") == "kpl"


class TestEnsureUnitsAndConversions:
    def _make_grocy(self, existing_units=None, existing_conversions=None):
        grocy = MagicMock(spec=StorageClient)
        grocy.get_quantity_units.return_value = existing_units or []
        grocy.get_quantity_unit_conversions.return_value = existing_conversions or []
        grocy.create_quantity_unit.side_effect = lambda name, *a, **kw: (
            100 + len([c for c in grocy.create_quantity_unit.call_args_list])
        )
        grocy.create_quantity_unit_conversion.return_value = 200
        return grocy

    def test_creates_missing_units(self):
        from grocy_scraper_addon.main import _ensure_units_and_conversions
        grocy = self._make_grocy()
        result = _ensure_units_and_conversions(grocy)
        # Should have created all 9 standard units
        assert grocy.create_quantity_unit.call_count == len(
            [u for u in result if u != "piece"]
        )
        assert "g" in result
        assert "kg" in result
        assert "l" in result

    def test_skips_existing_units_by_description(self):
        from grocy_scraper_addon.main import _ensure_units_and_conversions
        existing = [{"id": 5, "name": "Gramma", "description": "g"}]
        grocy = self._make_grocy(existing_units=existing)
        result = _ensure_units_and_conversions(grocy)
        assert result["g"] == 5
        # Should not have created "g" again
        create_names = [c.args[0] for c in grocy.create_quantity_unit.call_args_list]
        assert "Gramma" not in create_names

    def test_skips_existing_units_by_name(self):
        from grocy_scraper_addon.main import _ensure_units_and_conversions
        existing = [{"id": 7, "name": "Gramma", "description": ""}]
        grocy = self._make_grocy(existing_units=existing)
        result = _ensure_units_and_conversions(grocy)
        assert result["g"] == 7

    def test_creates_global_conversions(self):
        from grocy_scraper_addon.main import _ensure_units_and_conversions, _GLOBAL_CONVERSIONS
        grocy = self._make_grocy()
        _ensure_units_and_conversions(grocy)
        assert grocy.create_quantity_unit_conversion.call_count == len(_GLOBAL_CONVERSIONS)

    def test_skips_existing_conversions(self):
        from grocy_scraper_addon.main import _ensure_units_and_conversions
        existing_units = [
            {"id": 1, "name": "Kilogramma", "description": "kg"},
            {"id": 2, "name": "Gramma", "description": "g"},
        ]
        existing_convs = [
            {"id": 10, "from_qu_id": 1, "to_qu_id": 2, "factor": 1000, "product_id": None},
        ]
        grocy = self._make_grocy(existing_units=existing_units, existing_conversions=existing_convs)
        _ensure_units_and_conversions(grocy)
        # The kg→g conversion should be skipped
        for call in grocy.create_quantity_unit_conversion.call_args_list:
            assert not (call.args[0] == 1 and call.args[1] == 2)

    def test_maps_piece_unit(self):
        from grocy_scraper_addon.main import _ensure_units_and_conversions
        existing = [{"id": 1, "name": "Piece", "description": ""}]
        grocy = self._make_grocy(existing_units=existing)
        result = _ensure_units_and_conversions(grocy)
        assert result.get("piece") == 1



@patch("grocy_scraper_addon.main._call_gemini_json")
class TestAiDetectPackageSizes:
    def _make_grocy(self, products=None, conversions=None):
        grocy = MagicMock(spec=StorageClient)
        grocy.get_quantity_unit_conversions.return_value = conversions or []
        grocy.create_quantity_unit_conversion.return_value = 100
        return grocy

    def test_creates_conversion_for_product_with_size(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_package_sizes
        mock_gemini.return_value = [
            {"product_id": 1, "amount": 1, "unit": "l"},
        ]
        products = [{"id": 1, "name": "Arla Kevytmaito 1L",
                     "active": True}]
        abbrev = {"piece": 10, "l": 20, "g": 30}
        grocy = self._make_grocy(products)
        count = _ai_detect_package_sizes(grocy, products, abbrev, "key", "model")
        assert count == 1
        grocy.create_quantity_unit_conversion.assert_called_once_with(
            10, 20, 1.0, product_id=1,
        )

    def test_skips_products_with_existing_conversions(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_package_sizes
        products = [{"id": 1, "name": "Maito 1L",
                     "active": True}]
        existing_convs = [{"id": 50, "from_qu_id": 10, "to_qu_id": 20,
                          "factor": 1.0, "product_id": 1}]
        grocy = self._make_grocy(products, conversions=existing_convs)
        abbrev = {"piece": 10, "l": 20}
        count = _ai_detect_package_sizes(grocy, products, abbrev, "key", "model")
        assert count == 0
        mock_gemini.assert_not_called()

    def test_skips_null_unit_from_gemini(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_package_sizes
        mock_gemini.return_value = [
            {"product_id": 1, "amount": None, "unit": None},
        ]
        products = [{"id": 1, "name": "Tuorejuusto",
                     "active": True}]
        abbrev = {"piece": 10, "g": 30}
        grocy = self._make_grocy(products)
        count = _ai_detect_package_sizes(grocy, products, abbrev, "key", "model")
        assert count == 0

    def test_no_piece_unit_returns_zero(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_package_sizes
        products = [{"id": 1, "name": "Test", "unit_id": None,
                     "active": True}]
        abbrev = {"g": 30}  # No "piece" or "kpl"
        grocy = self._make_grocy(products)
        count = _ai_detect_package_sizes(grocy, products, abbrev, "key", "model")
        assert count == 0
        mock_gemini.assert_not_called()

    def test_skips_parent_placeholders(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_package_sizes
        mock_gemini.return_value = []
        products = [{"id": 1, "name": "Maito",
                     "active": False}]
        abbrev = {"piece": 10, "l": 20}
        grocy = self._make_grocy(products)
        count = _ai_detect_package_sizes(grocy, products, abbrev, "key", "model")
        assert count == 0


@patch("grocy_scraper_addon.main._call_gemini_json")
class TestAiDetectDensityConversions:
    def _make_grocy(self, conversions=None):
        grocy = MagicMock(spec=StorageClient)
        grocy.get_quantity_unit_conversions.return_value = conversions or []
        grocy.create_quantity_unit_conversion.return_value = 100
        return grocy

    def test_creates_density_for_weight_only_product(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_density_conversions
        mock_gemini.return_value = [
            {"product_id": 1, "from_unit": "kg", "to_unit": "l", "factor": 1.67},
        ]
        conversions = [
            {"id": 50, "from_qu_id": 10, "to_qu_id": 30, "factor": 1.0, "product_id": 1},
        ]
        products = [{"id": 1, "name": "Vehnäjauho 2kg"}]
        abbrev = {"piece": 10, "g": 20, "kg": 30, "l": 40, "dl": 50, "ml": 60}
        grocy = self._make_grocy(conversions=conversions)
        count = _ai_detect_density_conversions(grocy, products, abbrev, "key", "model")
        # 1 primary (kg→l) + 5 derived (kg→dl, kg→ml, g→l, g→dl, g→ml)
        assert count == 6
        # Primary conversion is the first call
        grocy.create_quantity_unit_conversion.assert_any_call(
            30, 40, 1.67, product_id=1,
        )

    def test_skips_product_with_both_domains(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_density_conversions
        # Product has both weight and volume conversions already
        conversions = [
            {"id": 50, "from_qu_id": 10, "to_qu_id": 30, "factor": 2.0, "product_id": 1},  # piece→kg
            {"id": 51, "from_qu_id": 10, "to_qu_id": 40, "factor": 3.0, "product_id": 1},  # piece→l
        ]
        products = [{"id": 1, "name": "Maito"}]
        abbrev = {"piece": 10, "kg": 30, "l": 40}
        grocy = self._make_grocy(conversions=conversions)
        count = _ai_detect_density_conversions(grocy, products, abbrev, "key", "model")
        assert count == 0
        mock_gemini.assert_not_called()

    def test_no_candidates_is_noop(self, mock_gemini):
        from grocy_scraper_addon.main import _ai_detect_density_conversions
        products = [{"id": 1, "name": "Maito"}]
        abbrev = {"piece": 10, "l": 40}
        grocy = self._make_grocy()
        count = _ai_detect_density_conversions(grocy, products, abbrev, "key", "model")
        assert count == 0
        mock_gemini.assert_not_called()


class TestDeriveDensityConversions:
    def test_kg_to_l_derives_all_pairs(self):
        from grocy_scraper_addon.main import _derive_density_conversions
        derived = _derive_density_conversions("kg", "l", 1.67)
        pairs = {(f, t) for f, t, _ in derived}
        assert ("kg", "l") not in pairs  # primary excluded
        assert ("kg", "dl") in pairs
        assert ("kg", "ml") in pairs
        assert ("g", "l") in pairs
        assert ("g", "dl") in pairs
        assert ("g", "ml") in pairs
        # Check a specific factor: 1 kg = 16.7 dl
        kg_dl = next(f for fu, tu, f in derived if fu == "kg" and tu == "dl")
        assert abs(kg_dl - 16.7) < 0.01

    def test_l_to_kg_derives_all_pairs(self):
        from grocy_scraper_addon.main import _derive_density_conversions
        derived = _derive_density_conversions("l", "kg", 1.03)
        pairs = {(f, t) for f, t, _ in derived}
        assert ("l", "kg") not in pairs  # primary excluded
        assert ("kg", "l") in pairs
        # 1 l = 1.03 kg → 1 kg = 1/1.03 l ≈ 0.9709 l
        kg_l = next(f for fu, tu, f in derived if fu == "kg" and tu == "l")
        assert abs(kg_l - 0.9709) < 0.01


@patch("grocy_scraper_addon.main._fix_recipe_units", return_value=0)
@patch("grocy_scraper_addon.main._ai_detect_density_conversions", return_value=0)
@patch("grocy_scraper_addon.main._ai_detect_package_sizes", return_value=0)
@patch("grocy_scraper_addon.main._fix_broken_product_units", return_value=0)
@patch("grocy_scraper_addon.main._ensure_units_and_conversions")
class TestOptimizeUnits:
    def test_calls_pipeline_steps(self, mock_ensure, mock_fix_broken,
                                   mock_pkg, mock_density,
                                   mock_fix_recipes):
        from grocy_scraper_addon.main import _optimize_units
        grocy = MagicMock(spec=StorageClient)
        grocy.get_all_products.return_value = [{"id": 1, "name": "Maito"}]
        mock_ensure.return_value = {"g": 1, "l": 2}

        _optimize_units(grocy, "key", "model")

        mock_ensure.assert_called_once_with(grocy)
        mock_fix_broken.assert_called_once()
        mock_pkg.assert_called_once()
        mock_density.assert_called_once()
        mock_fix_recipes.assert_called_once()

    def test_stops_on_ensure_failure(self, mock_ensure, mock_fix_broken,
                                      mock_pkg, mock_density,
                                      mock_fix_recipes):
        from grocy_scraper_addon.main import _optimize_units
        grocy = MagicMock(spec=StorageClient)
        mock_ensure.side_effect = StorageAPIError("fail")

        result = _optimize_units(grocy, "key", "model")
        assert result == 0
        mock_fix_broken.assert_not_called()

    def test_no_products_skips_ai(self, mock_ensure, mock_fix_broken,
                                   mock_pkg, mock_density,
                                   mock_fix_recipes):
        from grocy_scraper_addon.main import _optimize_units
        grocy = MagicMock(spec=StorageClient)
        grocy.get_all_products.return_value = []
        mock_ensure.return_value = {"g": 1}

        _optimize_units(grocy, "key", "model")
        mock_pkg.assert_not_called()
        mock_density.assert_not_called()


class TestFixBrokenProductUnits:
    def _make_grocy(self, units, products, conversions=None):
        grocy = MagicMock(spec=StorageClient)
        grocy.get_quantity_units.return_value = units
        grocy.get_all_products.return_value = products
        grocy.get_quantity_unit_conversions.return_value = conversions or []
        return grocy

    def test_fixes_orphaned_weight_product(self):
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 5, "name": "Gramma", "description": "g"}]
        products = [
            {"id": 1, "name": "Vehnäjauho 2kg", "unit_id": 999},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"g": 5, "kg": 6, "kpl": 10}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        call_kwargs = grocy.update_product.call_args[1]
        assert call_kwargs["unit_id"] == 6  # kg detected from "2kg"

    def test_fixes_orphaned_volume_product(self):
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 5, "name": "Litra", "description": "l"}]
        products = [
            {"id": 1, "name": "Maito 1L", "unit_id": 999},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"l": 5, "kpl": 10}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        call_kwargs = grocy.update_product.call_args[1]
        # Only the orphaned fields should be updated
        assert "unit_id" in call_kwargs
        assert call_kwargs["unit_id"] == 5  # l detected from "1L"
        assert "qu_id_consume" not in call_kwargs  # removed field

    def test_defaults_to_kpl_for_packaged(self):
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 10, "name": "Kappale", "description": "kpl"}]
        products = [
            {"id": 1, "name": "Hapankorppu", "unit_id": 999},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"kpl": 10, "g": 5}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        call_kwargs = grocy.update_product.call_args[1]
        assert call_kwargs["unit_id"] == 10  # kpl for no size in name

    def test_no_orphans_is_noop(self):
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 5, "name": "Gramma", "description": "g"}]
        products = [
            {"id": 1, "name": "Maito", "unit_id": 5},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"g": 5}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 0
        grocy.update_product.assert_not_called()

    def test_handles_update_failure(self):
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 10, "name": "Kappale", "description": "kpl"}]
        products = [
            {"id": 1, "name": "Tuote", "unit_id": 999},
        ]
        grocy = self._make_grocy(units, products)
        grocy.update_product.side_effect = StorageAPIError("fail")
        abbrev = {"kpl": 10}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 0

    def test_fixes_null_empty_qu_fields(self):
        """Products with null/empty QU fields should also be repaired."""
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 10, "name": "Kappale", "description": "kpl"}]
        products = [
            {"id": 1, "name": "Tuote", "unit_id": None},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"kpl": 10}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        call_kwargs = grocy.update_product.call_args[1]
        assert call_kwargs["unit_id"] == 10

    def test_cleans_orphaned_conversions(self):
        """Conversions referencing deleted units should be removed."""
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 5, "name": "Kappale", "description": "kpl"}]
        # Conversion references deleted unit 999
        conversions = [
            {"id": 100, "from_qu_id": 999, "to_qu_id": 5, "factor": 1,
             "product_id": 1},
            {"id": 101, "from_qu_id": 5, "to_qu_id": 999, "factor": 1,
             "product_id": 1},
            {"id": 102, "from_qu_id": 5, "to_qu_id": 5, "factor": 1,
             "product_id": None},  # valid, should not be deleted
        ]
        products = [
            {"id": 1, "name": "Tuote", "unit_id": 5},
        ]
        grocy = self._make_grocy(units, products, conversions)
        abbrev = {"kpl": 5}
        _fix_broken_product_units(grocy, abbrev)
        # Should delete the 2 orphaned conversions
        assert grocy.delete_quantity_unit_conversion.call_count == 2
        deleted_ids = {
            call.args[0]
            for call in grocy.delete_quantity_unit_conversion.call_args_list
        }
        assert deleted_ids == {100, 101}

    def test_fixes_stocked_product_directly(self):
        """Storage has no stock constraint — product update always succeeds."""
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 10, "name": "Kilogramma", "description": "kg"}]
        products = [
            {"id": 1, "name": "Vehnäjauho 1 kg", "unit_id": 999},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"kg": 10, "kpl": 20}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        grocy.update_product.assert_called_once_with(1, unit_id=10)

    def test_fixes_product_with_volume_unit(self):
        """Product with volume unit in name gets correct unit_id."""
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [{"id": 17, "name": "Litra", "description": "l"}]
        products = [
            {"id": 50, "name": "Keiju rypsiöljy 0,5l", "unit_id": 999},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"l": 17, "kpl": 21}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        grocy.update_product.assert_called_once_with(50, unit_id=17)

    def test_parent_inherits_unit_from_children(self):
        """Parent product with no size hint should inherit QU from children."""
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [
            {"id": 17, "name": "Litra", "description": "l"},
            {"id": 21, "name": "Kappale", "description": "kpl"},
        ]
        products = [
            # Parent with orphaned QU — name has no size hint
            {"id": 51, "name": "Rypsiöljy", "unit_id": 999},
            # Child uses litra
            {"id": 50, "name": "Keiju rypsiöljy 0,5l",
             "parent_id": 51, "unit_id": 17},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"l": 17, "kpl": 21}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        call_kwargs = grocy.update_product.call_args[1]
        # Should inherit 'l' from child, not default to 'kpl'
        assert call_kwargs["unit_id"] == 17

    def test_parent_falls_back_to_kpl_without_children(self):
        """Parent product with no children and no size hint defaults to kpl."""
        from grocy_scraper_addon.main import _fix_broken_product_units
        units = [
            {"id": 17, "name": "Litra", "description": "l"},
            {"id": 21, "name": "Kappale", "description": "kpl"},
        ]
        products = [
            {"id": 51, "name": "Rypsiöljy", "unit_id": 999},
        ]
        grocy = self._make_grocy(units, products)
        abbrev = {"l": 17, "kpl": 21}
        fixed = _fix_broken_product_units(grocy, abbrev)
        assert fixed == 1
        call_kwargs = grocy.update_product.call_args[1]
        assert call_kwargs["unit_id"] == 21  # kpl fallback


class TestFixRecipeUnits:
    def _make_grocy(self, positions, products, units, conversions=None):
        grocy = MagicMock(spec=StorageClient)
        grocy.get_recipe_positions.return_value = positions
        grocy.get_all_products.return_value = products
        grocy.get_quantity_units.return_value = units
        grocy.get_quantity_unit_conversions.return_value = conversions or []
        return grocy

    def test_fixes_invalid_qu_id(self):
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 999, "amount": 1},
        ]
        products = [
            {"id": 10, "name": "Maito", "unit_id": 5},
        ]
        units = [{"id": 5, "name": "Kappale", "description": "kpl"}]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"kpl": 5}
        fixed = _fix_recipe_units(grocy, abbrev)
        assert fixed == 1
        grocy.update_recipe_position.assert_called_once_with(1, qu_id=5)

    def test_same_unit_is_noop(self):
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 5, "amount": 1},
        ]
        products = [{"id": 10, "name": "Maito", "unit_id": 5}]
        units = [{"id": 5, "name": "Kappale", "description": "kpl"}]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"kpl": 5}
        fixed = _fix_recipe_units(grocy, abbrev)
        assert fixed == 0
        grocy.update_recipe_position.assert_not_called()

    def test_skips_when_conversion_exists(self):
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 500},
        ]
        products = [{"id": 10, "name": "Vehnäjauho 2kg", "unit_id": 5}]
        units = [
            {"id": 5, "name": "Kappale", "description": "kpl"},
            {"id": 7, "name": "Gramma", "description": "g"},
        ]
        conversions = [
            {"id": 50, "from_qu_id": 5, "to_qu_id": 7, "factor": 2000.0, "product_id": 10},
        ]
        grocy = self._make_grocy(positions, products, units, conversions)
        abbrev = {"kpl": 5, "g": 7}
        fixed = _fix_recipe_units(grocy, abbrev)
        assert fixed == 0
        grocy.update_recipe_position.assert_not_called()

    def test_same_domain_units_skip(self):
        """Units in the same domain (e.g., g→kg) have global conversions."""
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 500},
        ]
        products = [{"id": 10, "name": "Vehnäjauho", "unit_id": 8}]
        units = [
            {"id": 7, "name": "Gramma", "description": "g"},
            {"id": 8, "name": "Kilogramma", "description": "kg"},
        ]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"g": 7, "kg": 8}
        fixed = _fix_recipe_units(grocy, abbrev)
        assert fixed == 0
        grocy.update_recipe_position.assert_not_called()

    def test_falls_back_to_stock_qu_when_no_conversion(self):
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 600},
        ]
        products = [{"id": 10, "name": "Turskafilee", "unit_id": 5}]
        units = [
            {"id": 5, "name": "Kappale", "description": "kpl"},
            {"id": 7, "name": "Gramma", "description": "g"},
        ]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"kpl": 5, "g": 7}
        fixed = _fix_recipe_units(grocy, abbrev)
        assert fixed == 1
        grocy.update_recipe_position.assert_called_once_with(1, qu_id=5)

    def test_empty_positions_is_noop(self):
        from grocy_scraper_addon.main import _fix_recipe_units
        grocy = MagicMock(spec=StorageClient)
        grocy.get_recipe_positions.return_value = []
        abbrev = {"kpl": 5}
        fixed = _fix_recipe_units(grocy, abbrev)
        assert fixed == 0



class TestCheckRecipesForUnitGaps:
    """Test _check_recipes_for_unit_gaps: scan recipes for cross-domain unit mismatches."""

    def _make_grocy(self, positions, products, units, conversions=None):
        grocy = MagicMock(spec=StorageClient)
        grocy.get_recipe_positions.return_value = positions
        grocy.get_all_products.return_value = products
        grocy.get_quantity_units.return_value = units
        grocy.get_quantity_unit_conversions.return_value = conversions or []
        return grocy

    def test_detects_cross_domain_gap(self):
        """Recipe uses dl (volume) but product only has kg (weight) conversions."""
        from grocy_scraper_addon.main import _check_recipes_for_unit_gaps
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 2},
        ]
        products = [{"id": 10, "name": "Vehnäjauho 1kg", "unit_id": 5}]
        units = [
            {"id": 5, "name": "Kappale", "description": "kpl"},
            {"id": 7, "name": "Desilitra", "description": "dl"},
            {"id": 8, "name": "Kilogramma", "description": "kg"},
        ]
        # Product has kpl→kg (package size) but no volume conversions
        conversions = [
            {"id": 50, "from_qu_id": 5, "to_qu_id": 8, "factor": 1.0, "product_id": 10},
        ]
        grocy = self._make_grocy(positions, products, units, conversions)
        abbrev = {"kpl": 5, "dl": 7, "kg": 8, "g": 9, "l": 10, "ml": 11}

        with patch("grocy_scraper_addon.main._ai_detect_density_conversions", return_value=3) as mock_density:
            result = _check_recipes_for_unit_gaps(grocy, {10}, abbrev, "key", "model")

        mock_density.assert_called_once()
        assert result == 3

    def test_no_gap_when_both_domains_present(self):
        """Product already has weight AND volume conversions — no gap."""
        from grocy_scraper_addon.main import _check_recipes_for_unit_gaps
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 2},
        ]
        products = [{"id": 10, "name": "Vehnäjauho 1kg", "unit_id": 5}]
        units = [
            {"id": 5, "name": "Kappale", "description": "kpl"},
            {"id": 7, "name": "Desilitra", "description": "dl"},
            {"id": 8, "name": "Kilogramma", "description": "kg"},
            {"id": 10, "name": "Litra", "description": "l"},
        ]
        conversions = [
            {"id": 50, "from_qu_id": 5, "to_qu_id": 8, "factor": 1.0, "product_id": 10},
            {"id": 51, "from_qu_id": 8, "to_qu_id": 10, "factor": 1.67, "product_id": 10},
        ]
        grocy = self._make_grocy(positions, products, units, conversions)
        abbrev = {"kpl": 5, "dl": 7, "kg": 8, "l": 10}
        result = _check_recipes_for_unit_gaps(grocy, {10}, abbrev, "key", "model")
        assert result == 0

    def test_skips_products_not_in_target_set(self):
        """Only checks products in the provided product_ids set."""
        from grocy_scraper_addon.main import _check_recipes_for_unit_gaps
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 99, "qu_id": 7, "amount": 2},
        ]
        products = [{"id": 99, "name": "Jauho", "unit_id": 5}]
        units = [
            {"id": 5, "name": "Kappale", "description": "kpl"},
            {"id": 7, "name": "Desilitra", "description": "dl"},
        ]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"kpl": 5, "dl": 7}
        # product_ids={10} — product 99 is not in set
        result = _check_recipes_for_unit_gaps(grocy, {10}, abbrev, "key", "model")
        assert result == 0


class TestFixRecipeUnitsWithDensity:
    """Test that _fix_recipe_units tries density creation before fallback."""

    def _make_grocy(self, positions, products, units, conversions=None):
        grocy = MagicMock(spec=StorageClient)
        grocy.get_recipe_positions.return_value = positions
        grocy.get_all_products.return_value = products
        grocy.get_quantity_units.return_value = units
        grocy.get_quantity_unit_conversions.return_value = conversions or []
        return grocy

    def test_attempts_density_for_cross_domain_gap(self):
        """When recipe uses dl and product stock is kg, try density before fallback."""
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 2},
        ]
        products = [{"id": 10, "name": "Vehnäjauho 1kg", "unit_id": 8}]
        units = [
            {"id": 7, "name": "Desilitra", "description": "dl"},
            {"id": 8, "name": "Kilogramma", "description": "kg"},
        ]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"dl": 7, "kg": 8, "g": 9, "l": 10, "ml": 11}

        # Simulate successful density creation: after _ai_detect_density_conversions,
        # re-fetch shows the new conversion
        def refresh_conversions():
            return [
                {"from_qu_id": 8, "to_qu_id": 10, "factor": 1.67, "product_id": 10},
                {"from_qu_id": 8, "to_qu_id": 7, "factor": 16.7, "product_id": 10},
            ]
        grocy.get_quantity_unit_conversions.side_effect = [
            [],  # first call — no conversions
            refresh_conversions(),  # second call after density creation
        ]

        with patch("grocy_scraper_addon.main._ai_detect_density_conversions", return_value=2):
            fixed = _fix_recipe_units(grocy, abbrev, "gemini_key", "model")

        # No fallback needed — density conversion resolved the gap
        assert fixed == 0
        grocy.update_recipe_position.assert_not_called()

    def test_falls_back_when_density_fails(self):
        """When density creation fails, falls back to stock QU."""
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 2},
        ]
        products = [{"id": 10, "name": "Turskafilee", "unit_id": 8}]
        units = [
            {"id": 7, "name": "Desilitra", "description": "dl"},
            {"id": 8, "name": "Kilogramma", "description": "kg"},
        ]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"dl": 7, "kg": 8, "g": 9, "l": 10, "ml": 11}

        with patch("grocy_scraper_addon.main._ai_detect_density_conversions", return_value=0):
            fixed = _fix_recipe_units(grocy, abbrev, "gemini_key", "model")

        # Density failed — should fall back to stock QU
        assert fixed == 1
        grocy.update_recipe_position.assert_called_once_with(1, qu_id=8)

    def test_no_density_without_gemini_key(self):
        """Without gemini credentials, skips density and falls back directly."""
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 7, "amount": 2},
        ]
        products = [{"id": 10, "name": "Vehnäjauho 1kg", "unit_id": 8}]
        units = [
            {"id": 7, "name": "Desilitra", "description": "dl"},
            {"id": 8, "name": "Kilogramma", "description": "kg"},
        ]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"dl": 7, "kg": 8}

        # No gemini_api_key → no density attempt
        fixed = _fix_recipe_units(grocy, abbrev)
        assert fixed == 1
        grocy.update_recipe_position.assert_called_once_with(1, qu_id=8)

    def test_density_for_weight_recipe_volume_product(self):
        """Recipe uses g (weight) but product has l (volume) conversions."""
        from grocy_scraper_addon.main import _fix_recipe_units
        positions = [
            {"id": 1, "recipe_id": 1, "product_id": 10, "qu_id": 9, "amount": 500},
        ]
        products = [{"id": 10, "name": "Maito 1L", "unit_id": 10}]
        units = [
            {"id": 9, "name": "Gramma", "description": "g"},
            {"id": 10, "name": "Litra", "description": "l"},
        ]
        grocy = self._make_grocy(positions, products, units)
        abbrev = {"g": 9, "l": 10, "kg": 8, "dl": 7, "ml": 11}

        with patch("grocy_scraper_addon.main._ai_detect_density_conversions", return_value=2) as mock_d:
            # After density creation, conversion exists
            grocy.get_quantity_unit_conversions.side_effect = [
                [],
                [{"from_qu_id": 10, "to_qu_id": 8, "factor": 1.03, "product_id": 10},
                 {"from_qu_id": 9, "to_qu_id": 10, "factor": 0.00097, "product_id": 10}],
            ]
            fixed = _fix_recipe_units(grocy, abbrev, "key", "model")

        mock_d.assert_called_once()
        assert fixed == 0


class TestIncrementalDensityAndRecipeCheck:
    """Test that incremental optimize calls density detection and recipe gap check."""

    @patch("grocy_scraper_addon.main._check_recipes_for_unit_gaps")
    @patch("grocy_scraper_addon.main._ai_detect_density_conversions")
    @patch("grocy_scraper_addon.main._ai_detect_package_sizes")
    @patch("grocy_scraper_addon.main._fix_broken_product_units", return_value=0)
    @patch("grocy_scraper_addon.main._ensure_units_and_conversions")
    def test_incremental_calls_density_and_recipe_check(
        self, mock_ensure, mock_fix_broken, mock_pkg, mock_density, mock_recipe_check,
    ):
        """After package sizes, incremental should call density + recipe check."""
        from grocy_scraper_addon.main import _ai_optimize_products

        mock_ensure.return_value = {"kpl": 5, "kg": 8, "g": 9, "l": 10, "dl": 7}
        mock_pkg.return_value = 1
        mock_density.return_value = 2
        mock_recipe_check.return_value = 1

        grocy = MagicMock(spec=StorageClient)
        grocy.get_all_products.return_value = [
            {"id": 10, "name": "Vehnäjauho 1kg", "unit_id": 5,
             "parent_id": None, "location_id": 1,
             "product_group_id": None},
        ]
        grocy.get_all_barcodes.return_value = []
        grocy.get_product_groups.return_value = []
        grocy.add_stock.return_value = None

        # product_ids=[10] triggers incremental mode (not full)
        result = _ai_optimize_products(
            grocy, "gemini_key", "model",
            product_ids=[10],
        )

        # All should be called
        mock_fix_broken.assert_called_once()
        mock_pkg.assert_called_once()
        mock_density.assert_called_once()
        mock_recipe_check.assert_called_once()
