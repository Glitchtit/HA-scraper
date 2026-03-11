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
            "--location-id", "2",
            "--quantity-unit-id", "2",
        ])
        assert args.discover is True
        assert args.bbuddy_url == "https://bb.example.com"
        assert args.bbuddy_key == "BBKEY"

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
            "--bbuddy-key", "K",
            "--location-id", "1", "--quantity-unit-id", "1",
        ])
        assert args.bbuddy_url == "https://env-bb.example.com"

    def test_bbuddy_key_from_env(self, monkeypatch):
        monkeypatch.setenv("BARCODEBDY_API", "env-bb-key")
        args = parse_args([
            "--discover", "--store", "N110",
            "--grocy-url", "u", "--grocy-key", "k",
            "--bbuddy-url", "https://bb.example.com",
            "--location-id", "1", "--quantity-unit-id", "1",
        ])
        assert args.bbuddy_key == "env-bb-key"


# ---------------------------------------------------------------------------
# _validate_args – --discover mode
# ---------------------------------------------------------------------------

class TestValidateArgsDiscover:
    def _base_discover_args(self, **overrides):
        from argparse import Namespace
        defaults = dict(
            discover=True, query=None, browse=False,
            sort=False, date=False,
            store="N110",
            grocy_url="https://grocy.example.com",
            grocy_key="KEY",
            bbuddy_url="https://bb.example.com",
            bbuddy_key="BBKEY",
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

    def test_missing_bbuddy_key_fails(self):
        from main import _validate_args
        assert _validate_args(self._base_discover_args(bbuddy_key="")) == 1

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
    def test_no_unknowns_returns_0(self, MockBB, MockGrocy, MockScraper):
        MockBB.return_value.get_unknown_barcodes.return_value = []
        MockGrocy.return_value.get_all_barcodes.return_value = []

        from main import _discover_products
        from argparse import Namespace
        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
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
    def test_found_product_creates_and_stocks(self, MockBB, MockGrocy, MockScraper):
        from grocy_scraper.barcodebuddy_client import UnknownBarcode
        from main import _discover_products
        from argparse import Namespace

        bb_instance = MockBB.return_value
        bb_instance.get_unknown_barcodes.return_value = [
            UnknownBarcode(id="42", barcode="6410405082657", amount="1"),
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
        grocy_instance.create_product.assert_called_once()
        grocy_instance.add_stock.assert_called_once_with(99, amount=1.0)
        bb_instance.delete_barcode.assert_called_once_with("42")

    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_not_found_on_kruoka_skips(self, MockBB, MockGrocy, MockScraper):
        from grocy_scraper.barcodebuddy_client import UnknownBarcode
        from main import _discover_products
        from argparse import Namespace

        bb_instance = MockBB.return_value
        bb_instance.get_unknown_barcodes.return_value = [
            UnknownBarcode(id="42", barcode="0000000000000", amount="1"),
        ]

        grocy_instance = MockGrocy.return_value
        grocy_instance.get_all_barcodes.return_value = []

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([])

        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
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

    @patch("main.KRuokaScraper")
    @patch("main.GrocyClient")
    @patch("main.BarcodeBuddyClient")
    def test_bb_fetch_error_returns_1(self, MockBB, MockGrocy, MockScraper):
        from grocy_scraper.barcodebuddy_client import BarcodeBuddyError
        from main import _discover_products
        from argparse import Namespace

        MockGrocy.return_value.get_all_barcodes.return_value = []
        MockBB.return_value.get_unknown_barcodes.side_effect = BarcodeBuddyError("fail")

        args = Namespace(
            bbuddy_url="https://bb.example.com",
            bbuddy_key="KEY",
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
