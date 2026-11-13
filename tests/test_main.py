"""Integration tests for the main CLI entry point."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from addon.main import (
    main, parse_args, sync_product, _parse_store_ids,
    _sync_availability, _register_stores,
)
from scraper.scraper import Product
from scraper.storage_client import StorageAPIError, StorageClient


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_query_mode(self):
        args = parse_args(["--store", "N110", "--query", "maito",
                           "--storage-url", "https://storage.example.com"])
        assert args.query == "maito"
        assert not args.browse

    def test_browse_mode(self):
        args = parse_args(["--store", "N110", "--browse",
                           "--storage-url", "https://storage.example.com"])
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
        from addon.main import _env_int
        with patch.dict("os.environ", {"MY_VAR": "5"}):
            assert _env_int("MY_VAR") == 5

    def test_returns_none_when_unset(self):
        from addon.main import _env_int
        with patch.dict("os.environ", {}, clear=True):
            assert _env_int("MY_VAR") is None

    def test_returns_none_for_empty_string(self):
        from addon.main import _env_int
        with patch.dict("os.environ", {"MY_VAR": ""}):
            assert _env_int("MY_VAR") is None

    def test_raises_on_non_numeric(self):
        from addon.main import _env_int
        with patch.dict("os.environ", {"MY_VAR": "abc"}):
            with pytest.raises(ValueError):
                _env_int("MY_VAR")


# ---------------------------------------------------------------------------
# sync_product
# ---------------------------------------------------------------------------

class TestSyncProduct:
    def _storage(self):
        g = MagicMock(spec=StorageClient)
        g.get_product_by_barcode.return_value = None
        g.create_product.return_value = 99
        return g

    def test_skips_product_without_ean(self):
        storage = self._storage()
        product = Product(name="Tuote", ean="")
        result = sync_product(
            product, storage,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=set()
        )
        assert result is False
        storage.create_product.assert_not_called()

    def test_skips_known_barcode(self):
        storage = self._storage()
        product = Product(name="Maito", ean="111")
        known = {"111"}
        result = sync_product(
            product, storage,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=known
        )
        assert result is False
        storage.create_product.assert_not_called()

    def test_creates_new_product(self):
        storage = self._storage()
        product = Product(name="Maito", ean="999")
        result = sync_product(
            product, storage,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=set()
        )
        assert result is True
        storage.create_product.assert_called_once_with(
            name="Maito", description="", location_id=None, unit_id=None
        )
        storage.add_barcode.assert_called_once_with(99, "999")

    def test_skips_existing_barcode_from_storage_api(self):
        storage = self._storage()
        storage.get_product_by_barcode.return_value = {"id": 5, "name": "Existing"}
        product = Product(name="Maito", ean="555")
        result = sync_product(
            product, storage,
            location_id=None, quantity_unit_id=None,
            skip_existing=True, known_barcodes=set()
        )
        assert result is False
        storage.create_product.assert_not_called()

    def test_create_product_error_raises(self):
        storage = self._storage()
        storage.create_product.side_effect = StorageAPIError("fail")
        product = Product(name="Voi", ean="777")
        with pytest.raises(StorageAPIError):
            sync_product(
                product, storage,
                location_id=None, quantity_unit_id=None,
                skip_existing=False, known_barcodes=set()
            )

    def test_add_barcode_error_deletes_product_and_raises(self):
        storage = self._storage()
        storage.add_barcode.side_effect = StorageAPIError("fail")
        product = Product(name="Voi", ean="777")
        known = set()
        with pytest.raises(StorageAPIError):
            sync_product(
                product, storage,
                location_id=None, quantity_unit_id=None,
                skip_existing=False, known_barcodes=known
            )
        storage.delete_product.assert_called_once_with(99)
        assert known == set()

    @patch("addon.main.requests.get")
    def test_uploads_image_when_enabled(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8image-data"
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        storage = self._storage()
        product = Product(name="Maito", ean="123", image_url="https://img.example.com/photo.jpg")
        sync_product(
            product, storage,
            location_id=1, quantity_unit_id=1,
            skip_existing=False, known_barcodes=set(),
            upload_images=True,
        )
        storage.upload_product_image.assert_called_once_with(
            99, "123.jpg", b"\xff\xd8image-data", content_type="image/jpeg"
        )

    def test_no_image_upload_by_default(self):
        storage = self._storage()
        product = Product(name="Maito", ean="123", image_url="https://img.example.com/photo.jpg")
        sync_product(
            product, storage,
            location_id=1, quantity_unit_id=1,
            skip_existing=False, known_barcodes=set(),
        )
        storage.upload_product_image.assert_not_called()

    @patch("addon.main.requests.get")
    def test_image_download_failure_does_not_break_sync(self, mock_get):
        import requests as req
        mock_get.side_effect = req.RequestException("timeout")

        storage = self._storage()
        product = Product(name="Maito", ean="123", image_url="https://img.example.com/photo.jpg")
        result = sync_product(
            product, storage,
            location_id=1, quantity_unit_id=1,
            skip_existing=False, known_barcodes=set(),
            upload_images=True,
        )
        assert result is True
        storage.create_product.assert_called_once()
        storage.upload_product_image.assert_not_called()


# ---------------------------------------------------------------------------
# _image_extension helper
# ---------------------------------------------------------------------------

class TestImageExtension:
    def test_known_mime_types(self):
        from addon.main import _image_extension
        assert _image_extension("image/jpeg", "") == ".jpg"
        assert _image_extension("image/png", "") == ".png"
        assert _image_extension("image/webp", "") == ".webp"

    def test_unknown_mime_falls_back_to_url(self):
        from addon.main import _image_extension
        assert _image_extension("application/octet-stream", "https://example.com/img.png?w=100") == ".png"

    def test_unknown_mime_no_ext_defaults_to_jpg(self):
        from addon.main import _image_extension
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
        with patch("addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter(products)
            rc = main(["--store", "N110", "--browse", "--dry-run"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Maito" in out
        assert "111" in out

    def test_dry_run_passes_use_graphql_true(self, capsys):
        """By default, KRuokaScraper is constructed with use_graphql=True."""
        with patch("addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter([])
            main(["--store", "N110", "--browse", "--dry-run"])

        _, kwargs = MockScraper.call_args
        assert kwargs.get("use_graphql", True) is True

    def test_no_graphql_flag_passes_false(self, capsys):
        """--no-graphql passes use_graphql=False to KRuokaScraper."""
        with patch("addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.return_value = iter([])
            main(["--store", "N110", "--browse", "--dry-run", "--no-graphql"])

        _, kwargs = MockScraper.call_args
        assert kwargs.get("use_graphql") is False


# ---------------------------------------------------------------------------
# main – missing Storage config
# ---------------------------------------------------------------------------

class TestMainMissingStorage:
    def test_missing_storage_url(self, capsys):
        rc = main(["--store", "N110", "--browse"])
        assert rc == 1


# ---------------------------------------------------------------------------
# _validate_args
# ---------------------------------------------------------------------------

class TestValidateArgs:
    def test_no_mode_fails(self):
        from addon.main import _validate_args
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
        from addon.main import _validate_args
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


class TestParseArgsDeleteAll:
    def test_delete_all_flag(self):
        args = parse_args([
            "--delete-all",
            "--storage-url", "https://storage.example.com",
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
            storage_url="https://storage.example.com",
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
        from addon.main import _validate_args
        assert _validate_args(self._base_args()) == 0

    def test_missing_storage_url_fails(self):
        from addon.main import _validate_args
        assert _validate_args(self._base_args(storage_url="")) == 1

    def test_store_not_required(self):
        from addon.main import _validate_args
        assert _validate_args(self._base_args(store="")) == 0


# ---------------------------------------------------------------------------
# _delete_all_products
# ---------------------------------------------------------------------------

class TestDeleteAllProducts:
    @patch("addon.main.StorageClient")
    def test_empty_database_returns_0(self, MockStorage):
        from addon.main import _delete_all_products
        storage = MockStorage.return_value
        storage.get_all_products.return_value = []
        assert _delete_all_products(storage) == 0
        storage.delete_product.assert_not_called()

    @patch("addon.main.StorageClient")
    def test_deletes_all_products(self, MockStorage):
        from addon.main import _delete_all_products
        storage = MockStorage.return_value
        storage.get_all_products.return_value = [
            {"id": 1, "name": "Milk"},
            {"id": 2, "name": "Bread"},
            {"id": 3, "name": "Eggs"},
        ]
        assert _delete_all_products(storage) == 0
        assert storage.delete_product.call_count == 3
        storage.delete_product.assert_any_call(1)
        storage.delete_product.assert_any_call(2)
        storage.delete_product.assert_any_call(3)

    @patch("addon.main.StorageClient")
    def test_fetch_error_returns_1(self, MockStorage):
        from addon.main import _delete_all_products
        storage = MockStorage.return_value
        storage.get_all_products.side_effect = StorageAPIError("connection refused")
        assert _delete_all_products(storage) == 1

    @patch("addon.main.StorageClient")
    def test_partial_failure_returns_1(self, MockStorage):
        from addon.main import _delete_all_products
        storage = MockStorage.return_value
        storage.get_all_products.return_value = [
            {"id": 1, "name": "Milk"},
            {"id": 2, "name": "Bread"},
        ]
        storage.delete_product.side_effect = [None, StorageAPIError("failed")]
        assert _delete_all_products(storage) == 1
        assert storage.delete_product.call_count == 2


# ---------------------------------------------------------------------------
# parse_args – --discover flag
# ---------------------------------------------------------------------------

class TestParseArgsDiscover:
    def test_discover_flag(self):
        args = parse_args([
            "--discover",
            "--store", "N110",
            "--storage-url", "https://storage.example.com",
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
            storage_url="https://storage.example.com",
            location_id=2,
            quantity_unit_id=2,
            dry_run=False,
            gemini_api_key="",
        )
        defaults.update(overrides)
        return Namespace(**defaults)

    def test_valid_discover_passes(self):
        from addon.main import _validate_args
        assert _validate_args(self._base_discover_args()) == 0

    def test_missing_store_fails(self):
        from addon.main import _validate_args
        assert _validate_args(self._base_discover_args(store="")) == 1

    def test_missing_storage_url_fails(self):
        from addon.main import _validate_args
        assert _validate_args(self._base_discover_args(storage_url="")) == 1

    def test_missing_location_id_ok(self):
        """location_id=None is now allowed (auto-detected by StorageClient)."""
        from addon.main import _validate_args
        assert _validate_args(self._base_discover_args(location_id=None)) == 0

    def test_missing_quantity_unit_id_ok(self):
        """quantity_unit_id=None is now allowed (auto-detected by StorageClient)."""
        from addon.main import _validate_args
        assert _validate_args(self._base_discover_args(quantity_unit_id=None)) == 0


# ---------------------------------------------------------------------------
# _discover_products
# ---------------------------------------------------------------------------

class TestDiscoverProducts:
    @patch("addon.main.KRuokaScraper")
    @patch("addon.main.StorageClient")
    def test_no_pending_returns_0(self, MockStorage, MockScraper):
        storage_instance = MockStorage.return_value
        storage_instance.get_all_barcodes.return_value = []
        storage_instance.get_barcode_queue.return_value = []

        from addon.main import _discover_products
        from argparse import Namespace
        args = Namespace(
            storage_url="https://storage.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 0
        assert discovered_ids == []

    @patch("addon.main.KRuokaScraper")
    @patch("addon.main.StorageClient")
    def test_unknown_barcode_searched_on_kruoka(self, MockStorage, MockScraper):
        from addon.main import _discover_products
        from argparse import Namespace

        storage_instance = MockStorage.return_value
        storage_instance.get_all_barcodes.return_value = []
        storage_instance.get_barcode_queue.return_value = [
            {"id": 42, "barcode": "6410405082657", "source": "scanner"},
        ]
        storage_instance.get_product_by_barcode.side_effect = [
            None,  # sync_product check
            {"id": 99, "name": "Maito"},  # post-creation lookup
        ]
        storage_instance.create_product.return_value = 99

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([
            Product(name="Pirkka kevytmaito 1l", ean="6410405082657"),
        ])

        args = Namespace(
            storage_url="https://storage.example.com",
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
        storage_instance.create_product.assert_called_once()
        storage_instance.add_stock.assert_called_once_with(99, amount=1.0)
        storage_instance.update_barcode_queue_item.assert_called_once_with(
            42, status="done", result_product_id=99,
        )

    @patch("addon.main.KRuokaScraper")
    @patch("addon.main.StorageClient")
    def test_barcode_already_in_storage_stocks_existing_product(self, MockStorage, MockScraper):
        """A queued barcode that already maps to a product is stocked, not re-created."""
        from addon.main import _discover_products
        from argparse import Namespace

        storage_instance = MockStorage.return_value
        storage_instance.get_all_barcodes.return_value = [{"barcode": "6410405082657"}]
        storage_instance.get_barcode_queue.return_value = [
            {"id": 42, "barcode": "6410405082657", "source": "scanner"},
        ]
        storage_instance.get_product_by_barcode.return_value = {"id": 5, "name": "Maito"}
        # Storage rejects a second product's barcode with 409.
        storage_instance.add_barcode.side_effect = StorageAPIError("409 already exists")
        MockScraper.return_value.search.return_value = iter([
            Product(name="Pirkka kevytmaito 1l", ean="6410405082657"),
        ])

        args = Namespace(
            storage_url="https://storage.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 0
        assert discovered_ids == [5]
        storage_instance.create_product.assert_not_called()
        storage_instance.add_stock.assert_called_once_with(5, amount=1.0)
        storage_instance.update_barcode_queue_item.assert_called_once_with(
            42, status="done", result_product_id=5,
        )

    @patch("addon.main.skaupat_lookup")
    @patch("addon.main.KRuokaScraper")
    @patch("addon.main.StorageClient")
    def test_not_found_on_kruoka_or_skaupat_skips(self, MockStorage, MockScraper, mock_sk):
        from addon.main import _discover_products
        from argparse import Namespace

        storage_instance = MockStorage.return_value
        storage_instance.get_all_barcodes.return_value = []
        storage_instance.get_barcode_queue.return_value = [
            {"id": 42, "barcode": "0000000000000", "source": "scanner"},
        ]

        scraper_instance = MockScraper.return_value
        scraper_instance.search.return_value = iter([])
        mock_sk.return_value = None

        args = Namespace(
            storage_url="https://storage.example.com",
            store="N110",
            use_graphql=True,
            location_id=2,
            quantity_unit_id=2,
            upload_images=False,
        )
        rc, discovered_ids = _discover_products(args)
        assert rc == 0
        assert discovered_ids == []
        storage_instance.create_product.assert_not_called()
        storage_instance.update_barcode_queue_item.assert_called_once_with(
            42, status="error",
            error_message="Product not found for EAN 0000000000000",
        )
        mock_sk.assert_called_once_with("0000000000000")

    @patch("addon.main.skaupat_lookup")
    @patch("addon.main.KRuokaScraper")
    @patch("addon.main.StorageClient")
    def test_skaupat_fallback_creates_product(self, MockStorage, MockScraper, mock_sk):
        """When K-Ruoka has no match, S-kaupat result is used."""
        from scraper.skaupat_client import SKaupatProduct
        from addon.main import _discover_products
        from argparse import Namespace

        storage_instance = MockStorage.return_value
        storage_instance.get_all_barcodes.return_value = []
        storage_instance.get_barcode_queue.return_value = [
            {"id": 77, "barcode": "6414893095588", "source": "scanner"},
        ]
        storage_instance.get_product_by_barcode.side_effect = [
            None,
            {"id": 88, "name": "Kotimaista luomukananmunat M6"},
        ]
        storage_instance.create_product.return_value = 88

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
            storage_url="https://storage.example.com",
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
        storage_instance.create_product.assert_called_once()
        call_args = storage_instance.create_product.call_args
        assert call_args[1]["name"] == "Kotimaista luomukananmunat M6"
        storage_instance.add_stock.assert_called_once_with(88, amount=1.0)
        storage_instance.update_barcode_queue_item.assert_called_once_with(
            77, status="done", result_product_id=88,
        )

    @patch("addon.main.KRuokaScraper")
    @patch("addon.main.StorageClient")
    def test_queue_fetch_error_returns_1(self, MockStorage, MockScraper):
        from scraper.storage_client import StorageAPIError
        from addon.main import _discover_products
        from argparse import Namespace

        MockStorage.return_value.get_all_barcodes.return_value = []
        MockStorage.return_value.get_barcode_queue.side_effect = StorageAPIError("fail")

        args = Namespace(
            storage_url="https://storage.example.com",
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
        with patch("addon.main.KRuokaScraper") as MockScraper:
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

        with patch("addon.main.KRuokaScraper", side_effect=make_scraper):
            rc = main(["--store", "N110,N137", "--browse", "--dry-run"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Maito" in out

    def test_all_stores_fail_raises(self, capsys):
        """When all stores fail, the last error is raised."""
        with patch("addon.main.KRuokaScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.browse.side_effect = RuntimeError("all fail")
            with pytest.raises(RuntimeError, match="all fail"):
                main(["--store", "N110,N137", "--browse", "--dry-run"])

    def test_single_store_still_works(self, capsys):
        """Single store (no comma) still works as before."""
        products = [Product(name="Kerma", ean="222")]
        with patch("addon.main.KRuokaScraper") as MockScraper:
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

    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_discover_tries_second_store(
        self, MockScraper, MockStorage, mock_skaupat,
    ):
        from addon.main import _discover_products

        # Set up barcode queue to return one pending barcode.
        storage_instance = MockStorage.return_value
        storage_instance.get_all_barcodes.return_value = []
        storage_instance.get_barcode_queue.return_value = [
            {"id": 10, "barcode": "123", "source": "scanner"},
        ]
        storage_instance.get_product_by_barcode.return_value = {"id": 1}

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
            storage_url="https://storage.example.com",
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

    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_update_tries_second_store(
        self, MockScraper, MockStorage, mock_skaupat,
    ):
        from addon.main import _update_products

        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Old Milk"},
        ]
        storage_instance.get_all_barcodes.return_value = [
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
            storage_url="https://storage.example.com",
            use_graphql=True,
            upload_images=False,
            max_products=None,
        )
        rc = _update_products(args)
        assert MockScraper.call_count == 2
        assert rc == 0
        storage_instance.update_product.assert_called_once()


class TestUpdateSearXNGConfidence:
    """SearXNG fallback names are low-confidence: they may fill placeholder
    names but must never overwrite an existing real product name."""

    def _args(self):
        return Namespace(
            store="K532",
            storage_url="https://storage.example.com",
            use_graphql=True,
            upload_images=False,
            max_products=None,
            searxng_url="http://searxng:8080",
        )

    def _no_hit_scraper(self, MockScraper):
        def make_scraper(**kwargs):
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            s.search.return_value = iter([])
            return s
        MockScraper.side_effect = make_scraper

    @patch("addon.main.searxng_lookup")
    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_searxng_does_not_overwrite_real_name(
        self, MockScraper, MockStorage, mock_skaupat, mock_searxng,
    ):
        from addon.main import _update_products
        from scraper.searxng_client import SearXNGProduct

        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Aranciata Rossa Sparkling Water"},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "8002270626883"},
        ]
        self._no_hit_scraper(MockScraper)
        mock_searxng.return_value = SearXNGProduct(
            name="Junk Title From The Web", ean="8002270626883",
        )

        rc = _update_products(self._args())
        assert rc == 0
        # No name overwrite → no update at all (no price configured either).
        for call in storage_instance.update_product.call_args_list:
            assert "name" not in call.kwargs

    @patch("addon.main.searxng_lookup")
    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_searxng_fills_placeholder_name(
        self, MockScraper, MockStorage, mock_skaupat, mock_searxng,
    ):
        from addon.main import _update_products
        from scraper.searxng_client import SearXNGProduct

        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Unknown product (8002270626883)"},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "8002270626883"},
        ]
        self._no_hit_scraper(MockScraper)
        mock_searxng.return_value = SearXNGProduct(
            name="San Pellegrino Aranciata Rossa 330ml", ean="8002270626883",
        )

        rc = _update_products(self._args())
        assert rc == 0
        storage_instance.update_product.assert_called_once()
        _, kwargs = storage_instance.update_product.call_args
        assert kwargs["name"] == "San Pellegrino Aranciata Rossa 330ml"

    @patch("addon.main.searxng_lookup")
    @patch("addon.main.skaupat_lookup")
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_skaupat_still_overwrites_real_name(
        self, MockScraper, MockStorage, mock_skaupat, mock_searxng,
    ):
        """Real store hits (S-kaupat) keep full overwrite rights."""
        from addon.main import _update_products
        from scraper.skaupat_client import SKaupatProduct

        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Old stale name"},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "123"},
        ]
        self._no_hit_scraper(MockScraper)
        mock_skaupat.return_value = SKaupatProduct(
            name="Fresh S-kaupat name", ean="123",
        )

        rc = _update_products(self._args())
        assert rc == 0
        mock_searxng.assert_not_called()
        _, kwargs = storage_instance.update_product.call_args
        assert kwargs["name"] == "Fresh S-kaupat name"


class TestUpdateTrafficReduction:
    """Availability sweeps use GraphQL; kr-api is reserved for prices and
    gated by a per-run budget with staleness rotation."""

    def _args(self):
        return Namespace(
            store="K532,N141",
            storage_url="https://storage.example.com",
            use_graphql=True,
            upload_images=False,
            max_products=None,
        )

    @patch.dict("os.environ", {"FLARESOLVERR_URL": "http://fs:8191"})
    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_availability_swept_via_graphql_not_krapi(
        self, MockScraper, MockStorage, mock_skaupat, tmp_path, monkeypatch,
    ):
        from addon.main import _update_products

        monkeypatch.setenv("SCRAPER_STATE_DIR", str(tmp_path))
        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Maito", "unit_price": None},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "123"},
        ]

        graphql_scrapers, krapi_scrapers = [], []

        def make_scraper(**kwargs):
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            s.search.return_value = iter([Product(name="Maito", ean="123")])
            s.check_store_availability.return_value = []
            (graphql_scrapers if kwargs.get("use_graphql", True)
             else krapi_scrapers).append(s)
            return s

        MockScraper.side_effect = make_scraper

        rc = _update_products(self._args())
        assert rc == 0
        assert any(s.check_store_availability.called for s in graphql_scrapers)
        assert not any(s.check_store_availability.called for s in krapi_scrapers)

    @patch.dict("os.environ", {
        "FLARESOLVERR_URL": "http://fs:8191", "KRAPI_PRICE_BUDGET": "1",
    })
    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_price_budget_caps_krapi_lookups(
        self, MockScraper, MockStorage, mock_skaupat, tmp_path, monkeypatch,
    ):
        from addon.main import _update_products

        monkeypatch.setenv("SCRAPER_STATE_DIR", str(tmp_path))
        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Maito", "unit_price": None},
            {"id": 2, "name": "Leipä", "unit_price": None},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "111"},
            {"product_id": 2, "barcode": "222"},
        ]

        krapi_scrapers = []

        def make_scraper(**kwargs):
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            # Only kr-api scrapers carry prices (like the real backends).
            price = None if kwargs.get("use_graphql", True) else 2.0
            s.search.side_effect = lambda q, max_products=None, _p=price: iter(
                [Product(name="X", ean=q, price=_p)]
            )
            s.check_store_availability.return_value = []
            if not kwargs.get("use_graphql", True):
                krapi_scrapers.append(s)
            return s

        MockScraper.side_effect = make_scraper

        rc = _update_products(self._args())
        assert rc == 0
        # Budget=1: exactly one product got a kr-api price lookup.
        priced_eans = {
            call.args[0]
            for s in krapi_scrapers for call in s.search.call_args_list
        }
        assert len(priced_eans) == 1
        # State file records the refreshed product for rotation.
        import json as _json
        state = _json.loads((tmp_path / "price_refresh.json").read_text())
        assert len(state) >= 1

    @patch.dict("os.environ", {"FLARESOLVERR_URL": "http://fs:8191"})
    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_fresh_products_skip_price_lookup(
        self, MockScraper, MockStorage, mock_skaupat, tmp_path, monkeypatch,
    ):
        import json as _json
        import time as _time
        from addon.main import _update_products

        monkeypatch.setenv("SCRAPER_STATE_DIR", str(tmp_path))
        # Product 1 was price-refreshed moments ago -> within TTL, skip.
        (tmp_path / "price_refresh.json").write_text(
            _json.dumps({"1": _time.time()})
        )
        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Maito", "unit_price": None},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "111"},
        ]

        krapi_scrapers = []

        def make_scraper(**kwargs):
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            s.search.return_value = iter([Product(name="Maito", ean="111")])
            s.check_store_availability.return_value = []
            if not kwargs.get("use_graphql", True):
                krapi_scrapers.append(s)
            return s

        MockScraper.side_effect = make_scraper

        rc = _update_products(self._args())
        assert rc == 0
        assert not any(s.search.called for s in krapi_scrapers)


class TestUpdatePrices:
    """Prices are fetched via the kr-api backend and written as unit_price."""

    @patch.dict("os.environ", {"FLARESOLVERR_URL": "http://flaresolverr:8191"})
    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_update_writes_unit_price_from_krapi(
        self, MockScraper, MockStorage, mock_skaupat,
    ):
        from addon.main import _update_products

        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Pirkka kevytmaito", "unit_price": None},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "123"},
        ]

        # Build order in _update_products: name scraper (GraphQL, no price)
        # first, then the kr-api price scraper (use_graphql=False) which carries
        # the mobilescan price.
        scrapers_built: list[dict] = []

        def make_scraper(**kwargs):
            scrapers_built.append(kwargs)
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            if kwargs.get("use_graphql", True):
                s.search.return_value = iter([Product(name="Pirkka kevytmaito", ean="123")])
            else:
                s.search.return_value = iter([
                    Product(name="Pirkka kevytmaito", ean="123", price=1.35),
                ])
            return s

        MockScraper.side_effect = make_scraper

        args = Namespace(
            store="K532",
            storage_url="https://storage.example.com",
            use_graphql=True,
            upload_images=False,
            max_products=None,
        )
        rc = _update_products(args)

        assert rc == 0
        # A price scraper (use_graphql=False) was created.
        assert any(k.get("use_graphql") is False for k in scrapers_built)
        storage_instance.update_product.assert_called_once()
        _, kwargs = storage_instance.update_product.call_args
        assert kwargs["unit_price"] == 1.35
        assert kwargs["unit_price_currency"] == "EUR"

    @patch.dict("os.environ", {}, clear=False)
    @patch("addon.main.skaupat_lookup", return_value=None)
    @patch("addon.main.StorageClient")
    @patch("addon.main.KRuokaScraper")
    def test_no_cf_bypass_skips_price_scrapers(
        self, MockScraper, MockStorage, mock_skaupat,
    ):
        import os
        from addon.main import _update_products

        # Ensure no CF bypass is configured.
        os.environ.pop("FLARESOLVERR_URL", None)
        os.environ.pop("CF_CLEARANCE", None)

        storage_instance = MockStorage.return_value
        storage_instance.get_all_products.return_value = [
            {"id": 1, "name": "Old Milk", "unit_price": None},
        ]
        storage_instance.get_all_barcodes.return_value = [
            {"product_id": 1, "barcode": "123"},
        ]

        def make_scraper(**kwargs):
            s = MagicMock()
            s.store_id = kwargs.get("store_id", "")
            s.search.return_value = iter([Product(name="New Milk", ean="123")])
            return s

        MockScraper.side_effect = make_scraper

        args = Namespace(
            store="K532",
            storage_url="https://storage.example.com",
            use_graphql=True,
            upload_images=False,
            max_products=None,
        )
        rc = _update_products(args)

        assert rc == 0
        # Only the single GraphQL name scraper is built — no price scraper.
        assert MockScraper.call_count == 1
        # Name still updated, but no price written.
        _, kwargs = storage_instance.update_product.call_args
        assert "unit_price" not in kwargs


class TestSyncAvailability:
    def test_writes_merged_entries(self):
        from unittest.mock import MagicMock
        from scraper.scraper import StoreAvailability

        storage = MagicMock()
        s1 = MagicMock()
        s1.check_store_availability.return_value = [
            StoreAvailability(store_id="N110", available=True, price=2.35),
        ]
        s2 = MagicMock()
        s2.check_store_availability.return_value = [
            StoreAvailability(store_id="K532", available=False),
        ]
        _sync_availability(storage, 42, "6410405082657", [s1, s2])
        storage.set_product_availability.assert_called_once_with(42, [
            {"store_id": "N110", "available": True,
             "price": 2.35, "price_currency": "EUR"},
            {"store_id": "K532", "available": False},
        ])

    def test_first_scraper_wins_duplicate_stores(self):
        from unittest.mock import MagicMock
        from scraper.scraper import StoreAvailability

        storage = MagicMock()
        s1 = MagicMock()
        s1.check_store_availability.return_value = [
            StoreAvailability(store_id="N110", available=True, price=2.0),
        ]
        s2 = MagicMock()
        s2.check_store_availability.return_value = [
            StoreAvailability(store_id="N110", available=False),
        ]
        _sync_availability(storage, 1, "123", [s1, s2])
        entries = storage.set_product_availability.call_args[0][1]
        assert entries == [{"store_id": "N110", "available": True,
                            "price": 2.0, "price_currency": "EUR"}]

    def test_no_entries_no_write(self):
        from unittest.mock import MagicMock
        storage = MagicMock()
        scraper = MagicMock()
        scraper.check_store_availability.return_value = []
        _sync_availability(storage, 1, "123", [scraper])
        storage.set_product_availability.assert_not_called()

    def test_empty_ean_or_scrapers_noop(self):
        from unittest.mock import MagicMock
        storage = MagicMock()
        _sync_availability(storage, 1, "", [MagicMock()])
        _sync_availability(storage, 1, "123", [])
        storage.set_product_availability.assert_not_called()

    def test_scraper_exception_skipped_storage_error_swallowed(self):
        from unittest.mock import MagicMock
        from scraper.scraper import StoreAvailability
        from scraper.storage_client import StorageAPIError

        storage = MagicMock()
        storage.set_product_availability.side_effect = StorageAPIError("down")
        bad = MagicMock()
        bad.check_store_availability.side_effect = RuntimeError("cf wall")
        good = MagicMock()
        good.check_store_availability.return_value = [
            StoreAvailability(store_id="N110", available=True),
        ]
        # Must not raise despite both failure modes.
        _sync_availability(storage, 1, "123", [bad, good])
        storage.set_product_availability.assert_called_once()


class TestRegisterStores:
    def test_friendly_name_from_first_resolving_scraper(self):
        from unittest.mock import MagicMock

        storage = MagicMock()
        s1 = MagicMock()
        s1.fetch_store_name.return_value = None
        s2 = MagicMock()
        s2.fetch_store_name.return_value = "K-Citymarket Kupittaa"
        _register_stores(storage, ["N110"], [s1, s2])
        storage.upsert_store.assert_called_once_with("N110", "K-Citymarket Kupittaa")

    def test_fallback_to_raw_id(self):
        from unittest.mock import MagicMock

        storage = MagicMock()
        _register_stores(storage, ["N110"], [])
        storage.upsert_store.assert_called_once_with("N110", "N110")

    def test_errors_never_raise(self):
        from unittest.mock import MagicMock
        from scraper.storage_client import StorageAPIError

        storage = MagicMock()
        storage.upsert_store.side_effect = StorageAPIError("down")
        s = MagicMock()
        s.fetch_store_name.side_effect = RuntimeError("cf")
        _register_stores(storage, ["N110", "K532"], [s])  # must not raise
        assert storage.upsert_store.call_count == 2


# ---------------------------------------------------------------------------
# _price_from_entries
# ---------------------------------------------------------------------------

class TestPriceFromEntries:
    def test_first_available_priced_entry_wins(self):
        from addon.main import _price_from_entries

        entries = [
            {"store_id": "N110", "available": False},
            {"store_id": "K532", "available": True,
             "price": 1.5, "price_currency": "EUR"},
            {"store_id": "N137", "available": True,
             "price": 9.9, "price_currency": "EUR"},
        ]
        assert _price_from_entries(entries) == 1.5

    def test_unavailable_priced_entry_is_skipped(self):
        from addon.main import _price_from_entries

        entries = [
            {"store_id": "N110", "available": False,
             "price": 3.0, "price_currency": "EUR"},
            {"store_id": "K532", "available": True,
             "price": 4.0, "price_currency": "EUR"},
        ]
        assert _price_from_entries(entries) == 4.0

    def test_no_priced_entries_returns_none(self):
        from addon.main import _price_from_entries

        entries = [{"store_id": "N110", "available": True}]
        assert _price_from_entries(entries) is None

    def test_empty_entries_returns_none(self):
        from addon.main import _price_from_entries

        assert _price_from_entries([]) is None


# ---------------------------------------------------------------------------
# sync_product – price derived from the availability sweep (no duplicate
# kr-api lookup for the same EAN; see CLAUDE.md finding on CF-walled traffic)
# ---------------------------------------------------------------------------

class TestSyncProductSweepPricing:
    def _storage(self):
        g = MagicMock(spec=StorageClient)
        g.get_product_by_barcode.return_value = None
        g.create_product.return_value = 123
        return g

    def test_price_from_sweep_avoids_second_lookup(self):
        from scraper.scraper import StoreAvailability

        storage = self._storage()
        avail_scraper = MagicMock()
        avail_scraper.check_store_availability.return_value = [
            StoreAvailability(store_id="N110", available=True, price=3.49),
        ]
        price_scraper = MagicMock()
        product = Product(name="Maito", ean="123456")

        result = sync_product(
            product, storage,
            location_id=None, quantity_unit_id=None,
            skip_existing=False, known_barcodes=set(),
            price_scrapers=[price_scraper],
            avail_scrapers=[avail_scraper],
        )

        assert result is True
        storage.create_product.assert_called_once_with(
            name="Maito", description="", location_id=None, unit_id=None,
            unit_price=3.49, unit_price_currency="EUR",
        )
        # Price came from the availability sweep — _lookup_price's
        # per-store `.search()` must never be called for this EAN.
        price_scraper.search.assert_not_called()
        # Only one sweep of the availability endpoint total (reused for
        # both price derivation and the availability write below).
        avail_scraper.check_store_availability.assert_called_once_with("123456")
        storage.set_product_availability.assert_called_once_with(
            123, [{"store_id": "N110", "available": True,
                   "price": 3.49, "price_currency": "EUR"}],
        )

    def test_lookup_price_fallback_when_sweep_has_no_price(self):
        from scraper.scraper import StoreAvailability

        storage = self._storage()
        avail_scraper = MagicMock()
        avail_scraper.check_store_availability.return_value = [
            StoreAvailability(store_id="N110", available=False),
        ]
        price_scraper = MagicMock()
        priced = Product(name="Maito", ean="123456", price=2.10)
        price_scraper.search.return_value = iter([priced])
        product = Product(name="Maito", ean="123456")

        result = sync_product(
            product, storage,
            location_id=None, quantity_unit_id=None,
            skip_existing=False, known_barcodes=set(),
            price_scrapers=[price_scraper],
            avail_scrapers=[avail_scraper],
        )

        assert result is True
        price_scraper.search.assert_called_once()
        storage.create_product.assert_called_once_with(
            name="Maito", description="", location_id=None, unit_id=None,
            unit_price=2.10, unit_price_currency="EUR",
        )

    def test_no_avail_scrapers_still_uses_lookup_price(self):
        storage = self._storage()
        price_scraper = MagicMock()
        priced = Product(name="Maito", ean="123456", price=1.99)
        price_scraper.search.return_value = iter([priced])
        product = Product(name="Maito", ean="123456")

        sync_product(
            product, storage,
            location_id=None, quantity_unit_id=None,
            skip_existing=False, known_barcodes=set(),
            price_scrapers=[price_scraper],
            avail_scrapers=[],
        )

        price_scraper.search.assert_called_once()
        storage.create_product.assert_called_once_with(
            name="Maito", description="", location_id=None, unit_id=None,
            unit_price=1.99, unit_price_currency="EUR",
        )


# ---------------------------------------------------------------------------
# _discover_single_barcode (HA-stock scan of an unknown barcode)
# ---------------------------------------------------------------------------

@patch("addon.main._collect_availability", return_value=[])
@patch("addon.main._register_stores")
@patch("addon.main._make_price_scrapers", return_value=[])
@patch("addon.main.KRuokaScraper")
@patch("addon.main.StorageClient")
class TestDiscoverSingleBarcode:
    EAN = "6410405082657"

    def _run(self, MockStorage, MockScraper):
        from addon.main import _discover_single_barcode
        MockScraper.return_value.search.return_value = iter([
            Product(name="Pirkka kevytmaito 1l", ean=self.EAN),
        ])
        args = Namespace(
            storage_url="https://storage.example.com", store="N110",
            use_graphql=True, location_id=2, quantity_unit_id=2,
            upload_images=False,
        )
        return _discover_single_barcode(args, self.EAN)

    def test_happy_path_creates_and_stocks(self, MockStorage, MockScraper, *_):
        storage = MockStorage.return_value
        storage.get_product_by_barcode.side_effect = [
            None,  # already-known check
            None,  # sync_product check
            {"id": 99, "name": "Pirkka kevytmaito 1l"},  # post-creation lookup
        ]
        storage.create_product.return_value = 99
        result = self._run(MockStorage, MockScraper)
        assert result["success"] is True
        assert result["product_id"] == 99
        assert result["already_existed"] is False
        storage.add_barcode.assert_called_once_with(99, self.EAN)
        storage.add_stock.assert_called_once_with(99, amount=1.0)

    def test_already_known_barcode(self, MockStorage, MockScraper, *_):
        storage = MockStorage.return_value
        storage.get_product_by_barcode.return_value = {"id": 5, "name": "Maito"}
        result = self._run(MockStorage, MockScraper)
        assert result["success"] is True
        assert result["already_existed"] is True
        assert result["product_id"] == 5
        storage.create_product.assert_not_called()

    def test_create_failure_reports_error(self, MockStorage, MockScraper, *_):
        storage = MockStorage.return_value
        storage.get_product_by_barcode.return_value = None
        storage.create_product.side_effect = StorageAPIError("restarting")
        result = self._run(MockStorage, MockScraper)
        assert result["success"] is False
        assert "Pirkka kevytmaito 1l" in result["error"]
        storage.add_stock.assert_not_called()

    def test_barcode_failure_reports_error_and_removes_orphan(self, MockStorage, MockScraper, *_):
        storage = MockStorage.return_value
        storage.get_product_by_barcode.return_value = None
        storage.create_product.return_value = 99
        storage.add_barcode.side_effect = StorageAPIError("rejected")
        result = self._run(MockStorage, MockScraper)
        assert result["success"] is False
        storage.delete_product.assert_called_once_with(99)
        storage.add_stock.assert_not_called()

    def test_unresolvable_product_id_reports_error(self, MockStorage, MockScraper, *_):
        storage = MockStorage.return_value
        storage.get_product_by_barcode.side_effect = [None, None, StorageAPIError("down")]
        storage.create_product.return_value = 99
        result = self._run(MockStorage, MockScraper)
        assert result["success"] is False
        storage.add_stock.assert_not_called()
