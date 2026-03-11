"""Integration tests for the main CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from main import main, parse_args, sync_product
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
            sort=True, date=False,
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            gemini_api_key="GEMINI_KEY",
            query=None, browse=False,
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
            sort=False, date=False,
            query=None, browse=False,
            dry_run=False,
            grocy_url="", grocy_key="", gemini_api_key="",
            store="", location_id=None, quantity_unit_id=None,
        )
        assert _validate_args(args) == 1

    def test_dry_run_alone_fails(self):
        from main import _validate_args
        from argparse import Namespace
        args = Namespace(
            sort=False, date=False,
            query=None, browse=False,
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


# ---------------------------------------------------------------------------
# _ai_sort_products
# ---------------------------------------------------------------------------

class TestAiSortProducts:
    def _make_grocy(self, products, locations):
        g = MagicMock(spec=GrocyClient)
        g.get_all_products.return_value = products
        g.get_locations.return_value = locations
        g.update_product.return_value = None
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

    @patch("main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini):
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

    @patch("main._call_gemini")
    def test_invalid_json_skips_batch(self, mock_gemini):
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
