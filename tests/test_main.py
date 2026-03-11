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
