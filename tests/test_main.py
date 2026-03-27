"""Integration tests for the main CLI entry point."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from main import main, parse_args, sync_product, _parse_store_ids
from grocy_scraper.scraper import Product
from grocy_scraper.grocy_client import GrocyAPIError, GrocyClient


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_query_mode(self):
        args = parse_args(["--store", "N110", "--query", "maito",
                           "--grocy-url", "https://grocy.example.com",
                           "--grocy-key", "KEY"])
        assert args.query == "maito"
        assert not args.browse

    def test_browse_mode(self):
        args = parse_args(["--store", "N110", "--browse",
                           "--grocy-url", "https://grocy.example.com",
                           "--grocy-key", "KEY"])
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
                           "--grocy-url", "u", "--grocy-key", "k",
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
        from main import _env_int
        with patch.dict("os.environ", {"MY_VAR": "5"}):
            assert _env_int("MY_VAR") == 5

    def test_returns_none_when_unset(self):
        from main import _env_int
        with patch.dict("os.environ", {}, clear=True):
            assert _env_int("MY_VAR") is None

    def test_returns_none_for_empty_string(self):
        from main import _env_int
        with patch.dict("os.environ", {"MY_VAR": ""}):
            assert _env_int("MY_VAR") is None

    def test_raises_on_non_numeric(self):
        from main import _env_int
        with patch.dict("os.environ", {"MY_VAR": "abc"}):
            with pytest.raises(ValueError):
                _env_int("MY_VAR")


# ---------------------------------------------------------------------------
# sync_product
# ---------------------------------------------------------------------------

class TestSyncProduct:
    def _grocy(self):
        g = MagicMock(spec=GrocyClient)
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
            name="Maito", description="", location_id=None, quantity_unit_id=None
        )
        grocy.add_barcode.assert_called_once_with(99, "999", quantity_unit_id=None)

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
        grocy.create_product.side_effect = GrocyAPIError("fail")
        product = Product(name="Voi", ean="777")
        result = sync_product(
            product, grocy,
            location_id=None, quantity_unit_id=None,
            skip_existing=False, known_barcodes=set()
        )
        assert result is False

    @patch("main.requests.get")
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

    @patch("main.requests.get")
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
        from main import _image_extension
        assert _image_extension("image/jpeg", "") == ".jpg"
        assert _image_extension("image/png", "") == ".png"
        assert _image_extension("image/webp", "") == ".webp"

    def test_unknown_mime_falls_back_to_url(self):
        from main import _image_extension
        assert _image_extension("application/octet-stream", "https://example.com/img.png?w=100") == ".png"

    def test_unknown_mime_no_ext_defaults_to_jpg(self):
        from main import _image_extension
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
        with patch("main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter(products)
            rc = main(["--store", "N110", "--browse", "--dry-run"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Maito" in out
        assert "111" in out

    def test_dry_run_passes_use_graphql_true(self, capsys):
        """By default, KRuokaScraper is constructed with use_graphql=True."""
        with patch("main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter([])
            main(["--store", "N110", "--browse", "--dry-run"])

        _, kwargs = MockScraper.call_args
        assert kwargs.get("use_graphql", True) is True

    def test_no_graphql_flag_passes_false(self, capsys):
        """--no-graphql passes use_graphql=False to KRuokaScraper."""
        with patch("main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter([])
            main(["--store", "N110", "--browse", "--dry-run", "--no-graphql"])

        _, kwargs = MockScraper.call_args
        assert kwargs.get("use_graphql") is False


# ---------------------------------------------------------------------------
# main – missing Grocy config
# ---------------------------------------------------------------------------

class TestMainMissingGrocy:
    def test_missing_grocy_url(self, capsys):
        rc = main(["--store", "N110", "--browse", "--grocy-key", "K"])
        assert rc == 1

    def test_missing_grocy_key(self, capsys):
        rc = main(["--store", "N110", "--browse", "--grocy-url", "https://g.example.com"])
        assert rc == 1


# ---------------------------------------------------------------------------
# parse_args – AI flags
# ---------------------------------------------------------------------------

class TestParseArgsAIFlags:
    def test_sort_flag(self):
        args = parse_args([
            "--sort",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.sort is True
        assert args.date is False
        assert args.gemini_api_key == "GEMINI_KEY"

    def test_date_flag(self):
        args = parse_args([
            "--date",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.date is True
        assert args.sort is False

    def test_sort_and_date_together(self):
        args = parse_args([
            "--sort", "--date",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "GEMINI_KEY",
        ])
        assert args.sort is True
        assert args.date is True

    def test_group_flag(self):
        args = parse_args([
            "--group",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
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
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
        ])
        assert args.gemini_api_key == "env-key-123"

    def test_gemini_model_default(self):
        from main import _GEMINI_DEFAULT_MODEL
        args = parse_args([
            "--sort",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "KEY",
        ])
        assert args.gemini_model == _GEMINI_DEFAULT_MODEL

    def test_gemini_model_flag(self):
        args = parse_args([
            "--sort",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "KEY",
            "--gemini-model", "gemini-2.0-flash",
        ])
        assert args.gemini_model == "gemini-2.0-flash"

    def test_gemini_model_from_env(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-pro")
        args = parse_args([
            "--sort",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
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
            sort=True, date=False, group=False,
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
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
        from main import _validate_args
        assert _validate_args(self._base_ai_args()) == 0

    def test_missing_gemini_key_fails(self):
        from main import _validate_args
        args = self._base_ai_args(gemini_api_key="")
        assert _validate_args(args) == 1

    def test_missing_grocy_url_fails(self):
        from main import _validate_args
        args = self._base_ai_args(grocy_url="")
        assert _validate_args(args) == 1

    def test_missing_grocy_key_fails(self):
        from main import _validate_args
        args = self._base_ai_args(grocy_key="")
        assert _validate_args(args) == 1

    def test_no_mode_fails(self):
        from main import _validate_args
        from argparse import Namespace
        args = Namespace(
            sort=False, date=False, group=False,
            query=None, browse=False,
            discover=False, delete_all=False, update=False,
            dry_run=False,
            grocy_url="", grocy_key="", gemini_api_key="",
            store="", location_id=None, quantity_unit_id=None,
        )
        assert _validate_args(args) == 1

    def test_dry_run_alone_fails(self):
        from main import _validate_args
        from argparse import Namespace
        args = Namespace(
            sort=False, date=False, group=False,
            query=None, browse=False,
            discover=False, delete_all=False, update=False,
            dry_run=True,
            grocy_url="", grocy_key="", gemini_api_key="",
            store="N110", location_id=None, quantity_unit_id=None,
        )
        assert _validate_args(args) == 1


# ---------------------------------------------------------------------------
# _call_gemini
# ---------------------------------------------------------------------------

class TestCallGemini:
    @patch("main.requests.post")
    def test_returns_text_from_response(self, mock_post):
        from main import _call_gemini
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

    @patch("main.requests.post")
    def test_uses_specified_model_in_url(self, mock_post):
        from main import _call_gemini
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}]
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        _call_gemini("prompt", "api-key", model="gemini-2.0-flash")
        url = mock_post.call_args[0][0]
        assert "gemini-2.0-flash" in url

    @patch("main.requests.post")
    def test_http_error_raises(self, mock_post):
        import requests as req
        from main import _call_gemini
        mock_resp = MagicMock(status_code=400)
        mock_post.return_value = mock_resp
        mock_resp.raise_for_status.side_effect = req.HTTPError(response=mock_resp)
        with pytest.raises(GrocyAPIError, match="Gemini API error"):
            _call_gemini("prompt", "bad-key")

    @patch("main.requests.post")
    def test_unexpected_format_raises(self, mock_post):
        from main import _call_gemini
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}  # missing 'candidates'
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        with pytest.raises(GrocyAPIError, match="Unexpected Gemini"):
            _call_gemini("prompt", "key")


class TestCallGeminiJson:
    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_returns_parsed_json(self, mock_gemini, _mock_sleep):
        from main import _call_gemini_json
        mock_gemini.return_value = '{"1": 2}'
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        _mock_sleep.assert_not_called()

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_sanitizes_control_characters(self, mock_gemini, _mock_sleep):
        from main import _call_gemini_json
        mock_gemini.return_value = '{"1":\x02 2}'
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        _mock_sleep.assert_not_called()

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_retries_on_json_error(self, mock_gemini, mock_sleep):
        from main import _call_gemini_json
        mock_gemini.side_effect = ["not-json", '{"1": 2}']
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        assert mock_gemini.call_count == 2
        mock_sleep.assert_called_once()

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_retries_on_api_error(self, mock_gemini, mock_sleep):
        from main import _call_gemini_json
        mock_gemini.side_effect = [GrocyAPIError("HTML error"), '{"1": 2}']
        result = _call_gemini_json("prompt", "key")
        assert result == {"1": 2}
        assert mock_gemini.call_count == 2
        mock_sleep.assert_called_once()

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_raises_after_max_retries(self, mock_gemini, mock_sleep):
        import json as _json
        from main import _call_gemini_json
        mock_gemini.return_value = "not-json"
        with pytest.raises(_json.JSONDecodeError):
            _call_gemini_json("prompt", "key", max_retries=3)
        assert mock_gemini.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_exponential_backoff(self, mock_gemini, mock_sleep):
        import json as _json
        from main import _call_gemini_json
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
        g = MagicMock(spec=GrocyClient)
        g.get_all_products.return_value = products
        g.get_locations.return_value = locations
        g.update_product.return_value = None
        g.get_product_stock_locations.return_value = []
        g.transfer_stock.return_value = None
        return g

    @patch("main._call_gemini")
    def test_updates_products_with_ai_locations(self, mock_gemini):
        from main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}, {"id": 2, "name": "Pesuaine"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 3, "name": "Cleaning Cabinet"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2, "2": 3}'

        result = _ai_sort_products(grocy, "gemini-key")
        assert result == 2
        grocy.update_product.assert_any_call(1, location_id=2)
        grocy.update_product.assert_any_call(2, location_id=3)

    @patch("main._call_gemini")
    def test_transfers_stock_to_new_location(self, mock_gemini):
        from main import _ai_sort_products
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

    @patch("main._call_gemini")
    def test_skips_transfer_when_stock_already_at_target(self, mock_gemini):
        from main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2}'
        grocy.get_product_stock_locations.return_value = [
            {"location_id": 2, "amount": 4.0},
        ]

        _ai_sort_products(grocy, "gemini-key")
        grocy.transfer_stock.assert_not_called()

    @patch("main._call_gemini")
    def test_transfers_from_multiple_locations(self, mock_gemini):
        from main import _ai_sort_products
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

    @patch("main._call_gemini")
    def test_transfer_error_does_not_abort(self, mock_gemini):
        from main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}, {"id": 2, "name": "Pasta"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 5, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2, "2": 5}'
        grocy.get_product_stock_locations.side_effect = [
            [{"location_id": 5, "amount": 1.0}],
            [{"location_id": 2, "amount": 1.0}],
        ]
        grocy.transfer_stock.side_effect = [GrocyAPIError("fail"), None]

        result = _ai_sort_products(grocy, "gemini-key")
        assert result == 2
        assert grocy.transfer_stock.call_count == 2

    @patch("main._call_gemini")
    def test_stock_locations_error_continues(self, mock_gemini):
        from main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}, {"id": 2, "name": "Pasta"}]
        locations = [{"id": 2, "name": "Fridge"}, {"id": 5, "name": "Pantry"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = '{"1": 2, "2": 5}'
        grocy.get_product_stock_locations.side_effect = [
            GrocyAPIError("fail"),
            [],
        ]

        result = _ai_sort_products(grocy, "gemini-key")
        assert result == 2
        grocy.transfer_stock.assert_not_called()

    @patch("main._call_gemini")
    def test_no_locations_returns_zero(self, mock_gemini):
        from main import _ai_sort_products
        grocy = MagicMock(spec=GrocyClient)
        grocy.get_locations.return_value = []
        result = _ai_sort_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("main._call_gemini")
    def test_no_products_returns_zero(self, mock_gemini):
        from main import _ai_sort_products
        grocy = MagicMock(spec=GrocyClient)
        grocy.get_locations.return_value = [{"id": 2, "name": "Fridge"}]
        grocy.get_all_products.return_value = []
        result = _ai_sort_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini, _mock_sleep):
        from main import _ai_sort_products
        products = [{"id": 1, "name": "Maito"}]
        locations = [{"id": 2, "name": "Fridge"}]
        grocy = self._make_grocy(products, locations)
        mock_gemini.return_value = "not-json"

        result = _ai_sort_products(grocy, "key")
        assert result == 0
        grocy.update_product.assert_not_called()

    @patch("main._call_gemini")
    def test_grocy_error_on_locations_returns_zero(self, mock_gemini):
        from main import _ai_sort_products
        grocy = MagicMock(spec=GrocyClient)
        grocy.get_locations.side_effect = GrocyAPIError("fail")
        result = _ai_sort_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()


# ---------------------------------------------------------------------------
# _ai_assign_due_dates
# ---------------------------------------------------------------------------

class TestAiAssignDueDates:
    def _make_grocy(self, products):
        g = MagicMock(spec=GrocyClient)
        g.get_all_products.return_value = products
        g.update_product.return_value = None
        return g

    @patch("main._call_gemini")
    def test_updates_products_with_ai_dates(self, mock_gemini):
        from main import _ai_assign_due_dates
        products = [{"id": 1, "name": "Maito"}, {"id": 5, "name": "Pasta"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = '{"1": 14, "5": 1095}'

        result = _ai_assign_due_dates(grocy, "gemini-key")
        assert result == 2
        grocy.update_product.assert_any_call(1, default_best_before_days=14)
        grocy.update_product.assert_any_call(5, default_best_before_days=1095)

    @patch("main._call_gemini")
    def test_no_products_returns_zero(self, mock_gemini):
        from main import _ai_assign_due_dates
        grocy = MagicMock(spec=GrocyClient)
        grocy.get_all_products.return_value = []
        result = _ai_assign_due_dates(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini, _mock_sleep):
        from main import _ai_assign_due_dates
        products = [{"id": 1, "name": "Maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = "not-json"

        result = _ai_assign_due_dates(grocy, "key")
        assert result == 0
        grocy.update_product.assert_not_called()

    @patch("main._call_gemini")
    def test_grocy_error_returns_zero(self, mock_gemini):
        from main import _ai_assign_due_dates
        grocy = MagicMock(spec=GrocyClient)
        grocy.get_all_products.side_effect = GrocyAPIError("fail")
        result = _ai_assign_due_dates(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()


# ---------------------------------------------------------------------------
# _ai_group_products
# ---------------------------------------------------------------------------

class TestAiGroupProducts:
    _GROUP_MASTER_ID = 50
    _PARENT_GROUP_ID = 60

    def _make_grocy(self, products):
        g = MagicMock(spec=GrocyClient)
        g.get_all_products.return_value = products
        g.update_product.return_value = None
        g.create_product.return_value = 100
        # First call → "Group master"; subsequent calls → per-parent groups.
        g.ensure_product_group.side_effect = (
            lambda name: self._GROUP_MASTER_ID
            if name == "Group master"
            else self._PARENT_GROUP_ID
        )
        return g

    @patch("main._call_gemini")
    def test_groups_products_under_new_parent(self, mock_gemini):
        from main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka kevytmaito 1l"},
            {"id": 2, "name": "Valio kevytmaito 1l"},
            {"id": 3, "name": "Fazer ruisleipä"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = '{"1": "Maito", "2": "Maito", "3": null}'

        result = _ai_group_products(grocy, "gemini-key")
        assert result == 2
        # "Group master" and per-parent product groups should be ensured.
        grocy.ensure_product_group.assert_any_call("Group master")
        grocy.ensure_product_group.assert_any_call("Maito")
        # Parent product "Maito" should be created (not in existing products).
        grocy.create_product.assert_called_once_with(
            "Maito", location_id=None, quantity_unit_id=None,
        )
        # Parent should be configured with stock accumulation, "Group master"
        # product group, and hidden from the stock overview.
        grocy.update_product.assert_any_call(
            100,
            cumulate_min_stock_amount_of_sub_products=1,
            hide_on_stock_overview=1,
            product_group_id=self._GROUP_MASTER_ID,
        )
        # Child products should be updated with parent_product_id and the
        # per-parent product group.
        grocy.update_product.assert_any_call(
            1, parent_product_id=100, product_group_id=self._PARENT_GROUP_ID,
        )
        grocy.update_product.assert_any_call(
            2, parent_product_id=100, product_group_id=self._PARENT_GROUP_ID,
        )

    @patch("main._call_gemini")
    def test_reuses_existing_parent_product(self, mock_gemini):
        from main import _ai_group_products
        products = [
            {"id": 10, "name": "Maito"},  # Already exists as potential parent.
            {"id": 11, "name": "Pirkka kevytmaito 1l"},
        ]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = '{"10": null, "11": "Maito"}'

        result = _ai_group_products(grocy, "gemini-key")
        assert result == 1
        # Should NOT create a new product — reuse existing "Maito".
        grocy.create_product.assert_not_called()
        # Child should be assigned parent and per-parent product group.
        grocy.update_product.assert_any_call(
            11, parent_product_id=10, product_group_id=self._PARENT_GROUP_ID,
        )
        # Existing parent should still be updated with group / hide flags.
        grocy.update_product.assert_any_call(
            10,
            cumulate_min_stock_amount_of_sub_products=1,
            hide_on_stock_overview=1,
            product_group_id=self._GROUP_MASTER_ID,
        )

    @patch("main._call_gemini")
    def test_skips_already_grouped_products(self, mock_gemini):
        from main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka maito", "parent_product_id": 99},
            {"id": 2, "name": "Valio maito"},
        ]
        grocy = self._make_grocy(products)
        # Only product 2 should be sent to Gemini (product 1 already has parent).
        mock_gemini.return_value = '{"2": "Maito"}'

        result = _ai_group_products(grocy, "gemini-key")
        assert result == 1
        # Verify the prompt only included product 2.
        prompt_text = mock_gemini.call_args[0][0]
        assert "Valio maito" in prompt_text
        assert "Pirkka maito" not in prompt_text

    @patch("main._call_gemini")
    def test_no_products_returns_zero(self, mock_gemini):
        from main import _ai_group_products
        grocy = MagicMock(spec=GrocyClient)
        grocy.get_all_products.return_value = []
        result = _ai_group_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("main.time.sleep")
    @patch("main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini, _mock_sleep):
        from main import _ai_group_products
        products = [{"id": 1, "name": "Maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = "not-json"

        result = _ai_group_products(grocy, "key")
        assert result == 0
        grocy.update_product.assert_not_called()

    @patch("main._call_gemini")
    def test_grocy_error_returns_zero(self, mock_gemini):
        from main import _ai_group_products
        grocy = MagicMock(spec=GrocyClient)
        grocy.get_all_products.side_effect = GrocyAPIError("fail")
        result = _ai_group_products(grocy, "key")
        assert result == 0
        mock_gemini.assert_not_called()

    @patch("main._call_gemini")
    def test_does_not_set_product_as_own_parent(self, mock_gemini):
        """If Gemini maps a product to a parent with the same name, skip it."""
        from main import _ai_group_products
        products = [{"id": 5, "name": "Maito"}]
        grocy = self._make_grocy(products)
        # Gemini says product 5 should be under "Maito", but that IS product 5.
        mock_gemini.return_value = '{"5": "Maito"}'

        result = _ai_group_products(grocy, "key")
        assert result == 0
        # update_product should be called for cumulate flag but not for parent assignment.
        calls = [c for c in grocy.update_product.call_args_list
                 if "parent_product_id" in (c.kwargs or {})]
        assert len(calls) == 0

    @patch("main._call_gemini")
    def test_passes_location_and_quantity_unit(self, mock_gemini):
        from main import _ai_group_products
        products = [{"id": 1, "name": "Pirkka maito"}]
        grocy = self._make_grocy(products)
        mock_gemini.return_value = '{"1": "Maito"}'

        _ai_group_products(grocy, "key", location_id=5, quantity_unit_id=3)
        grocy.create_product.assert_called_once_with(
            "Maito", location_id=5, quantity_unit_id=3,
        )

    @patch("main._call_gemini")
    def test_group_without_product_group_on_ensure_failure(self, mock_gemini):
        """If ensure_product_group fails, grouping still works without group IDs."""
        from main import _ai_group_products
        products = [
            {"id": 1, "name": "Pirkka maito"},
            {"id": 2, "name": "Valio maito"},
        ]
        grocy = self._make_grocy(products)
        grocy.ensure_product_group.side_effect = GrocyAPIError("fail")
        mock_gemini.return_value = '{"1": "Maito", "2": "Maito"}'

        result = _ai_group_products(grocy, "key")
        assert result == 2
        # Parent should be updated without product_group_id.
        grocy.update_product.assert_any_call(
            100,
            cumulate_min_stock_amount_of_sub_products=1,
            hide_on_stock_overview=1,
        )
        # Children should be updated without product_group_id.
        grocy.update_product.assert_any_call(1, parent_product_id=100)
        grocy.update_product.assert_any_call(2, parent_product_id=100)

    @patch("main._call_gemini")
    def test_skips_products_that_are_already_parents(self, mock_gemini):
        """Products that already have sub-products must not be assigned a
        parent – that would create unsupported 2-level nesting in Grocy."""
        from main import _ai_group_products
        products = [
            {"id": 10, "name": "Sipuli"},           # already a parent
            {"id": 11, "name": "Punasipuli", "parent_product_id": 10},
            {"id": 12, "name": "Keltasipuli", "parent_product_id": 10},
            {"id": 20, "name": "Juusto"},            # truly ungrouped
        ]
        grocy = self._make_grocy(products)
        # Gemini only sees product 20 (the only truly ungrouped candidate).
        mock_gemini.return_value = '{"20": "Juustot"}'

        result = _ai_group_products(grocy, "gemini-key")
        assert result == 1
        # The prompt should NOT contain "Sipuli" (already a parent).
        prompt_text = mock_gemini.call_args[0][0]
        assert "Sipuli" not in prompt_text
        assert "Juusto" in prompt_text

    @patch("main._call_gemini")
    def test_skips_existing_parent_that_is_already_a_child(self, mock_gemini):
        """If an existing product matching the parent name is already a child
        of another product, it must not be reused as a parent."""
        from main import _ai_group_products
        products = [
            {"id": 1, "name": "Sipuli", "parent_product_id": 99},
            {"id": 2, "name": "Valkosipuli"},
        ]
        grocy = self._make_grocy(products)
        # Gemini says product 2 should be grouped under "Sipuli", but the
        # existing "Sipuli" (id 1) is already a child → must not reuse it.
        mock_gemini.return_value = '{"2": "Sipuli"}'

        result = _ai_group_products(grocy, "gemini-key")
        assert result == 0
        # Because the only candidate parent was skipped, no child assignment
        # should happen.
        parent_calls = [
            c for c in grocy.update_product.call_args_list
            if "parent_product_id" in (c.kwargs or {})
        ]
        assert len(parent_calls) == 0


# ---------------------------------------------------------------------------
# main – AI mode integration
# ---------------------------------------------------------------------------

class TestMainAIMode:
    @patch("main._ai_sort_products")
    @patch("main.GrocyClient")
    def test_sort_mode_calls_ai_sort(self, MockGrocy, mock_sort):
        mock_sort.return_value = 3
        rc = main([
            "--sort",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_sort.assert_called_once()

    @patch("main._ai_assign_due_dates")
    @patch("main.GrocyClient")
    def test_date_mode_calls_ai_dates(self, MockGrocy, mock_date):
        mock_date.return_value = 5
        rc = main([
            "--date",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_date.assert_called_once()

    @patch("main._ai_assign_due_dates")
    @patch("main._ai_sort_products")
    @patch("main.GrocyClient")
    def test_sort_and_date_together(self, MockGrocy, mock_sort, mock_date):
        mock_sort.return_value = 2
        mock_date.return_value = 2
        rc = main([
            "--sort", "--date",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_sort.assert_called_once()
        mock_date.assert_called_once()

    def test_missing_gemini_key_returns_1(self):
        rc = main([
            "--sort",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
        ])
        assert rc == 1

    @patch("main._ai_group_products")
    @patch("main.GrocyClient")
    def test_group_mode_calls_ai_group(self, MockGrocy, mock_group):
        mock_group.return_value = 4
        rc = main([
            "--group",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_group.assert_called_once()


class TestDiscoverChainsAI:
    """Discover mode should run sort/date/group when a Gemini key is set."""

    @patch("main._ai_group_products")
    @patch("main._ai_assign_due_dates")
    @patch("main._ai_sort_products")
    @patch("main._discover_products", return_value=0)
    @patch("main.GrocyClient")
    def test_discover_chains_sort_date_group(
        self, MockGrocy, mock_discover, mock_sort, mock_date, mock_group,
    ):
        mock_sort.return_value = 1
        mock_date.return_value = 1
        mock_group.return_value = 1
        rc = main([
            "--discover",
            "--store", "N110",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--bbuddy-url", "https://bb.example.com",
            "--bbuddy-user", "admin",
            "--bbuddy-password", "secret",
            "--location-id", "2",
            "--quantity-unit-id", "2",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 0
        mock_discover.assert_called_once()
        mock_sort.assert_called_once()
        mock_date.assert_called_once()
        mock_group.assert_called_once()

    @patch("main._ai_group_products")
    @patch("main._ai_assign_due_dates")
    @patch("main._ai_sort_products")
    @patch("main._discover_products", return_value=0)
    def test_discover_no_gemini_key_skips_ai(
        self, mock_discover, mock_sort, mock_date, mock_group,
    ):
        rc = main([
            "--discover",
            "--store", "N110",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--bbuddy-url", "https://bb.example.com",
            "--bbuddy-user", "admin",
            "--bbuddy-password", "secret",
            "--location-id", "2",
            "--quantity-unit-id", "2",
        ])
        assert rc == 0
        mock_discover.assert_called_once()
        mock_sort.assert_not_called()
        mock_date.assert_not_called()
        mock_group.assert_not_called()

    @patch("main._ai_group_products")
    @patch("main._ai_assign_due_dates")
    @patch("main._ai_sort_products")
    @patch("main._discover_products", return_value=1)
    def test_discover_failure_skips_ai(
        self, mock_discover, mock_sort, mock_date, mock_group,
    ):
        rc = main([
            "--discover",
            "--store", "N110",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--bbuddy-url", "https://bb.example.com",
            "--bbuddy-user", "admin",
            "--bbuddy-password", "secret",
            "--location-id", "2",
            "--quantity-unit-id", "2",
            "--gemini-api-key", "GEMINI",
        ])
        assert rc == 1
        mock_discover.assert_called_once()
        mock_sort.assert_not_called()
        mock_date.assert_not_called()
        mock_group.assert_not_called()


# ---------------------------------------------------------------------------
# parse_args – --delete-all flag
# ---------------------------------------------------------------------------

class TestParseArgsDeleteAll:
    def test_delete_all_flag(self):
        args = parse_args([
            "--delete-all",
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
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
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            query=None, browse=False, discover=False,
            sort=False, date=False, group=False,
            update=False,
            dry_run=False,
            store="",
            location_id=None,
            quantity_unit_id=None,
            bbuddy_url="", bbuddy_key="",
            bbuddy_user="", bbuddy_password="",
            gemini_api_key="",
        )
        defaults.update(overrides)
        return Namespace(**defaults)

    def test_valid_delete_all_passes(self):
        from main import _validate_args
        assert _validate_args(self._base_args()) == 0

    def test_missing_grocy_url_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_args(grocy_url="")) == 1

    def test_missing_grocy_key_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_args(grocy_key="")) == 1

    def test_store_not_required(self):
        from main import _validate_args
        assert _validate_args(self._base_args(store="")) == 0


# ---------------------------------------------------------------------------
# _delete_all_products
# ---------------------------------------------------------------------------

class TestDeleteAllProducts:
    @patch("main.GrocyClient")
    def test_empty_database_returns_0(self, MockGrocy):
        from main import _delete_all_products
        grocy = MockGrocy.return_value
        grocy.get_all_products.return_value = []
        assert _delete_all_products(grocy) == 0
        grocy.delete_product.assert_not_called()

    @patch("main.GrocyClient")
    def test_deletes_all_products(self, MockGrocy):
        from main import _delete_all_products
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

    @patch("main.GrocyClient")
    def test_fetch_error_returns_1(self, MockGrocy):
        from main import _delete_all_products
        grocy = MockGrocy.return_value
        grocy.get_all_products.side_effect = GrocyAPIError("connection refused")
        assert _delete_all_products(grocy) == 1

    @patch("main.GrocyClient")
    def test_partial_failure_returns_1(self, MockGrocy):
        from main import _delete_all_products
        grocy = MockGrocy.return_value
        grocy.get_all_products.return_value = [
            {"id": 1, "name": "Milk"},
            {"id": 2, "name": "Bread"},
        ]
        grocy.delete_product.side_effect = [None, GrocyAPIError("failed")]
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
            "--grocy-url", "https://grocy.example.com",
            "--grocy-key", "KEY",
            "--bbuddy-url", "https://bb.example.com",
            "--bbuddy-key", "BBKEY",
            "--bbuddy-user", "admin",
            "--bbuddy-password", "secret",
            "--location-id", "2",
            "--quantity-unit-id", "2",
        ])
        assert args.discover is True
        assert args.bbuddy_url == "https://bb.example.com"
        assert args.bbuddy_key == "BBKEY"
        assert args.bbuddy_user == "admin"
        assert args.bbuddy_password == "secret"

    def test_discover_mutually_exclusive_with_query(self):
        with pytest.raises(SystemExit):
            parse_args(["--discover", "--query", "maito"])

    def test_discover_mutually_exclusive_with_browse(self):
        with pytest.raises(SystemExit):
            parse_args(["--discover", "--browse"])

    def test_bbuddy_url_from_env(self, monkeypatch):
        monkeypatch.setenv("BARCODEBDY_URL", "https://env-bb.example.com")
        args = parse_args([
            "--discover", "--store", "N110",
            "--grocy-url", "u", "--grocy-key", "k",
            "--bbuddy-user", "u", "--bbuddy-password", "p",
            "--location-id", "1", "--quantity-unit-id", "1",
        ])
        assert args.bbuddy_url == "https://env-bb.example.com"

    def test_bbuddy_key_from_env(self, monkeypatch):
        monkeypatch.setenv("BARCODEBDY_API", "env-bb-key")
        args = parse_args([
            "--discover", "--store", "N110",
            "--grocy-url", "u", "--grocy-key", "k",
            "--bbuddy-url", "https://bb.example.com",
            "--bbuddy-user", "u", "--bbuddy-password", "p",
            "--location-id", "1", "--quantity-unit-id", "1",
        ])
        assert args.bbuddy_key == "env-bb-key"

    def test_bbuddy_user_from_env(self, monkeypatch):
        monkeypatch.setenv("BARCODEBDY_USER", "envuser")
        args = parse_args([
            "--discover", "--store", "N110",
            "--grocy-url", "u", "--grocy-key", "k",
            "--bbuddy-url", "https://bb.example.com",
            "--bbuddy-password", "p",
            "--location-id", "1", "--quantity-unit-id", "1",
        ])
        assert args.bbuddy_user == "envuser"

    def test_bbuddy_password_from_env(self, monkeypatch):
        monkeypatch.setenv("BARCODEBDY_PASSWORD", "envpass")
        args = parse_args([
            "--discover", "--store", "N110",
            "--grocy-url", "u", "--grocy-key", "k",
            "--bbuddy-url", "https://bb.example.com",
            "--bbuddy-user", "u",
            "--location-id", "1", "--quantity-unit-id", "1",
        ])
        assert args.bbuddy_password == "envpass"


# ---------------------------------------------------------------------------
# _validate_args – --discover mode
# ---------------------------------------------------------------------------

class TestValidateArgsDiscover:
    def _base_discover_args(self, **overrides):
        from argparse import Namespace
        defaults = dict(
            discover=True, query=None, browse=False,
            sort=False, date=False, group=False,
            delete_all=False, update=False,
            store="N110",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            bbuddy_url="https://bb.example.com",
            bbuddy_key="BBKEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            location_id=2,
            quantity_unit_id=2,
            dry_run=False,
            gemini_api_key="",
        )
        defaults.update(overrides)
        return Namespace(**defaults)

    def test_valid_discover_passes(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args()) == 0

    def test_missing_store_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(store="")) == 1

    def test_missing_grocy_url_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(grocy_url="")) == 1

    def test_missing_grocy_key_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(grocy_key="")) == 1

    def test_missing_bbuddy_url_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(bbuddy_url="")) == 1

    def test_missing_bbuddy_user_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(bbuddy_user="")) == 1

    def test_missing_bbuddy_password_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(bbuddy_password="")) == 1

    def test_missing_location_id_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(location_id=None)) == 1

    def test_missing_quantity_unit_id_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(quantity_unit_id=None)) == 1


# ---------------------------------------------------------------------------
# _discover_products
# ---------------------------------------------------------------------------

class TestDiscoverProducts:
    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_no_pending_returns_0(self, MockBB, MockGrocy, MockScraper):
        MockBB.return_value.get_pending_barcodes.return_value = []
        MockGrocy.return_value.get_all_barcodes.return_value = []

        from main import _discover_products
        from argparse import Namespace
        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc = _discover_products(args)
        assert rc == 0

    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_unknown_barcode_searched_on_kruoka(self, MockBB, MockGrocy, MockScraper):
        from grocy_scraper.barcodebuddy_client import PendingBarcode
        from main import _discover_products
        from argparse import Namespace

        bb_instance = MockBB.return_value
        bb_instance.get_pending_barcodes.return_value = [
            PendingBarcode(id="42", barcode="6410405082657", amount="1"),
        ]

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
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
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc = _discover_products(args)
        assert rc == 0
        # Unknown barcode → should search K-Ruoka.
        scraper_instance.search.assert_called_once()
        grocy_instance.create_product.assert_called_once()
        grocy_instance.add_stock.assert_called_once_with(99, amount=1.0)
        bb_instance.delete_barcode.assert_called_once_with("42")

    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_new_barcode_kruoka_overrides_bb_name(self, MockBB, MockGrocy, MockScraper):
        """K-Ruoka result takes priority over Barcode Buddy name."""
        from grocy_scraper.barcodebuddy_client import PendingBarcode
        from main import _discover_products
        from argparse import Namespace

        bb_instance = MockBB.return_value
        bb_instance.get_pending_barcodes.return_value = [
            PendingBarcode(id="10", barcode="6410405082657", amount="2",
                           name="BB Resolved Name"),
        ]

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
        grocy_instance.get_product_by_barcode.side_effect = [
            None,  # sync_product check
            {"id": 50, "name": "Pirkka kevytmaito 1l"},  # post-creation lookup
        ]
        grocy_instance.create_product.return_value = 50

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([
            Product(name="Pirkka kevytmaito 1l", ean="6410405082657"),
        ])

        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc = _discover_products(args)
        assert rc == 0
        # K-Ruoka search should always run, even when BB has a name.
        scraper_instance.search.assert_called_once()
        grocy_instance.create_product.assert_called_once()
        grocy_instance.add_stock.assert_called_once_with(50, amount=2.0)
        bb_instance.delete_barcode.assert_called_once_with("10")

    @patch("main.skaupat_lookup")
    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_new_barcode_falls_back_to_bb_name(self, MockBB, MockGrocy, MockScraper, mock_sk):
        """When K-Ruoka and S-kaupat have no match, fall back to BB name."""
        from grocy_scraper.barcodebuddy_client import PendingBarcode
        from main import _discover_products
        from argparse import Namespace

        bb_instance = MockBB.return_value
        bb_instance.get_pending_barcodes.return_value = [
            PendingBarcode(id="10", barcode="6410405082657", amount="1",
                           name="BB Fallback Name"),
        ]

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
        grocy_instance.get_product_by_barcode.side_effect = [
            None,
            {"id": 60, "name": "BB Fallback Name"},
        ]
        grocy_instance.create_product.return_value = 60

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([])  # K-Ruoka finds nothing.
        mock_sk.return_value = None  # S-kaupat finds nothing either.

        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc = _discover_products(args)
        assert rc == 0
        scraper_instance.search.assert_called_once()
        mock_sk.assert_called_once_with("6410405082657")
        grocy_instance.create_product.assert_called_once()
        bb_instance.delete_barcode.assert_called_once_with("10")

    @patch("main.skaupat_lookup")
    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_not_found_on_kruoka_or_skaupat_skips(self, MockBB, MockGrocy, MockScraper, mock_sk):
        from grocy_scraper.barcodebuddy_client import PendingBarcode
        from main import _discover_products
        from argparse import Namespace

        bb_instance = MockBB.return_value
        bb_instance.get_pending_barcodes.return_value = [
            PendingBarcode(id="42", barcode="0000000000000", amount="1"),
        ]

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([])
        mock_sk.return_value = None

        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc = _discover_products(args)
        assert rc == 0
        grocy_instance.create_product.assert_not_called()
        bb_instance.delete_barcode.assert_not_called()
        mock_sk.assert_called_once_with("0000000000000")

    @patch("main.skaupat_lookup")
    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_skaupat_fallback_creates_product(self, MockBB, MockGrocy, MockScraper, mock_sk):
        """When K-Ruoka has no match, S-kaupat result is used."""
        from grocy_scraper.barcodebuddy_client import PendingBarcode
        from grocy_scraper.skaupat_client import SKaupatProduct
        from main import _discover_products
        from argparse import Namespace

        bb_instance = MockBB.return_value
        bb_instance.get_pending_barcodes.return_value = [
            PendingBarcode(id="77", barcode="6414893095588", amount="1"),
        ]

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
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
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc = _discover_products(args)
        assert rc == 0
        mock_sk.assert_called_once_with("6414893095588")
        grocy_instance.create_product.assert_called_once()
        # Verify the product was created with S-kaupat data.
        call_args = grocy_instance.create_product.call_args
        assert call_args[1]["name"] == "Kotimaista luomukananmunat M6"
        grocy_instance.add_stock.assert_called_once_with(88, amount=1.0)
        bb_instance.delete_barcode.assert_called_once_with("77")

    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_bb_fetch_error_returns_1(self, MockBB, MockGrocy, MockScraper):
        from grocy_scraper.barcodebuddy_client import BarcodeBuddyError
        from main import _discover_products
        from argparse import Namespace

        MockGrocy.return_value.get_all_barcodes.return_value = []
        MockBB.return_value.get_pending_barcodes.side_effect = BarcodeBuddyError("fail")

        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc = _discover_products(args)
        assert rc == 1


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
        with patch("main.KRuokaScraper") as MockScraper:
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

        with patch("main.KRuokaScraper", side_effect=make_scraper):
            rc = main(["--store", "N110,N137", "--browse", "--dry-run"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Maito" in out

    def test_all_stores_fail_raises(self, capsys):
        """When all stores fail, the last error is raised."""
        with patch("main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.side_effect = RuntimeError("all fail")
            with pytest.raises(RuntimeError, match="all fail"):
                main(["--store", "N110,N137", "--browse", "--dry-run"])

    def test_single_store_still_works(self, capsys):
        """Single store (no comma) still works as before."""
        products = [Product(name="Kerma", ean="222")]
        with patch("main.KRuokaScraper") as MockScraper:
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

    @patch("main.skaupat_lookup", return_value=None)
    @patch("main.BarcodeBuddyClient")
    @patch("main.GrocyClient")
    @patch("main.KRuokaScraper")
    def test_discover_tries_second_store(
        self, MockScraper, MockGrocy, MockBBuddy, mock_skaupat,
    ):
        from main import _discover_products

        # Set up Barcode Buddy to return one pending barcode.
        bb_instance = MockBBuddy.return_value
        entry = MagicMock()
        entry.barcode = "123"
        entry.name = ""
        entry.amount = "1"
        entry.id = 10
        bb_instance.get_pending_barcodes.return_value = [entry]

        # Set up Grocy.
        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []
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
            sort=False, date=False, group=False,
            delete_all=False, update=False,
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
            bbuddy_user="admin",
            bbuddy_password="secret",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            store="N110,N137",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
            skip_existing=False,
        )
        rc = _discover_products(args)
        # Two scrapers created for the two stores.
        assert MockScraper.call_count == 2
        # The product was found via the second store and synced.
        assert rc == 0


# ---------------------------------------------------------------------------
# Multi-store fallback – _update_products
# ---------------------------------------------------------------------------

class TestMultiStoreUpdateFallback:
    """Test that update tries multiple stores for each barcode."""

    @patch("main.skaupat_lookup", return_value=None)
    @patch("main.GrocyClient")
    @patch("main.KRuokaScraper")
    def test_update_tries_second_store(
        self, MockScraper, MockGrocy, mock_skaupat,
    ):
        from main import _update_products

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
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            use_graphql=True,
            upload_images=False,
            max_products=None,
        )
        rc = _update_products(args)
        assert MockScraper.call_count == 2
        assert rc == 0
        grocy_instance.update_product.assert_called_once()
