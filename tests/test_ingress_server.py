"""Tests for the add-on ingress web server (addon/ingress_server.py)."""

from __future__ import annotations

import json
import logging
import time
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixture: the ingress_server module, as the add-on image imports it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingress_mod() -> ModuleType:
    import ingress_server

    return ingress_server


@pytest.fixture(autouse=True)
def _reset_storage_cache(ingress_mod: ModuleType) -> None:
    """Clear the cached storage URL between tests."""
    ingress_mod._cached_storage_url = None
    yield
    ingress_mod._cached_storage_url = None


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
        opts_file.write_text(json.dumps({"storage_url": "http://storage", "store_id": "N110"}))
        with mock.patch.object(ingress_mod, "_OPTIONS_PATH", str(opts_file)):
            result = ingress_mod._read_options()
        assert result["storage_url"] == "http://storage"
        assert result["store_id"] == "N110"


# ---------------------------------------------------------------------------
# _build_args
# ---------------------------------------------------------------------------


class TestBuildArgs:
    def test_basic_fields(self, ingress_mod: ModuleType) -> None:
        opts = {
            "store_id": "N110",
            "storage_url": "http://storage:9283",
        }
        ns = ingress_mod._build_args(opts)
        assert ns.store == "N110"
        assert ns.storage_url == "http://storage:9283"
        assert ns.location_id is None
        assert ns.quantity_unit_id is None

    def test_defaults(self, ingress_mod: ModuleType) -> None:
        ns = ingress_mod._build_args({})
        assert ns.store == ""
        assert ns.upload_images is True
        assert ns.searxng_url == ""


# ---------------------------------------------------------------------------
# _capture_logs
# ---------------------------------------------------------------------------


class TestCaptureLogs:
    def test_captures_info(self, ingress_mod: ModuleType) -> None:
        with ingress_mod._capture_logs() as logs:
            logging.getLogger("scraper").info("hello world")
        assert len(logs) >= 1
        assert logs[0]["level"] == "INFO"
        assert "hello world" in logs[0]["message"]

    def test_captures_debug(self, ingress_mod: ModuleType) -> None:
        with ingress_mod._capture_logs() as logs:
            logging.getLogger("main").debug("debug msg")
        assert any(r["level"] == "DEBUG" for r in logs)

    def test_handler_removed_after_context(self, ingress_mod: ModuleType) -> None:
        lgr = logging.getLogger("scraper")
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

    def test_fully_configured(self, ingress_mod: ModuleType) -> None:
        opts = {
            "storage_url": "http://storage",
            "store_id": "N110",
        }
        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            cfg = ingress_mod._handle_config()
        assert cfg == {"configured": True, "store_id": "N110"}


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
        from scraper.scraper import Product

        fake_products = [
            Product(name="Maito 1L", ean="6411234000001", description="Kevytmaito"),
        ]
        mock_scraper = mock.MagicMock()
        mock_scraper.search.return_value = iter(fake_products)

        with mock.patch.object(ingress_mod, "_read_options", return_value={"store_id": "N110"}):
            with mock.patch("scraper.scraper.KRuokaScraper", return_value=mock_scraper):
                result = ingress_mod._handle_search({"query": "maito", "max_products": 10})

        assert result["success"] is True
        assert len(result["products"]) == 1
        assert result["products"][0]["name"] == "Maito 1L"
        assert result["products"][0]["ean"] == "6411234000001"

    def test_search_falls_through_to_second_store(self, ingress_mod: ModuleType) -> None:
        """With several stores configured, hits from a later store are
        returned when the first store has none."""
        asked: list[str] = []

        def post(url, json=None, timeout=None):
            store_id = json["variables"]["storeId"]
            asked.append(store_id)
            results = [] if store_id == "K532" else [{
                "__typename": "Product",
                "ean": "6410405082657",
                "localizedName": {"finnish": "Pirkka kevytmaito 1l"},
            }]
            resp = mock.MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"data": {"productAndAssortmentSearchV2": {
                "results": results, "totalHits": len(results),
            }}}
            return resp

        session = mock.MagicMock()
        session.post.side_effect = post

        with mock.patch.object(ingress_mod, "_read_options", return_value={"store_id": "K532,N110"}):
            with mock.patch("scraper.scraper._make_graphql_session", return_value=session):
                result = ingress_mod._handle_search({"query": "kevytmaito"})

        assert asked == ["K532", "N110"]
        assert result["success"] is True
        assert [p["ean"] for p in result["products"]] == ["6410405082657"]

    def test_search_exception(self, ingress_mod: ModuleType) -> None:
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            with mock.patch(
                "scraper.scraper.KRuokaScraper",
                side_effect=RuntimeError("connection error"),
            ):
                result = ingress_mod._handle_search({"query": "test"})
        assert result["success"] is False
        assert "connection error" in result["error"]


# ---------------------------------------------------------------------------
# _handle_discover
# ---------------------------------------------------------------------------


EAN = "6410405082657"


@pytest.fixture
def options(ingress_mod: ModuleType):
    """The add-on options as the handlers read them."""
    opts = {"store_id": "K532", "storage_url": "http://storage"}
    with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
        yield opts


class TestHandleDiscover:
    def test_queue_item_is_created_stocked_and_marked_done(
        self, ingress_mod: ModuleType, world, options,
    ) -> None:
        world.kruoka.catalogue = {"K532": {EAN: "Pirkka kevytmaito 1l"}}
        world.storage.enqueue(EAN)

        result = ingress_mod._handle_discover()

        assert result["success"] is True
        assert result["skipped"] is False
        assert any("Pirkka kevytmaito 1l" in line["message"] for line in result["logs"])
        product_id = world.storage.barcodes[EAN]
        product = world.storage.products[product_id]
        assert product["name"] == "Pirkka kevytmaito 1l"
        assert product["picture_filename"] == f"{EAN}.jpg"
        assert world.downloads == [f"https://img.example/{EAN}"]
        assert world.storage.stock[product_id] == 1.0
        assert world.storage.queue[0]["status"] == "done"
        assert world.storage.queue[0]["result_product_id"] == product_id

    def test_barcode_found_nowhere_is_marked_as_error(
        self, ingress_mod: ModuleType, world, options,
    ) -> None:
        world.storage.enqueue(EAN)

        result = ingress_mod._handle_discover()

        assert result["success"] is True
        assert world.storage.products == {}
        assert world.storage.queue[0]["status"] == "error"
        assert EAN in world.storage.queue[0]["error_message"]

    def test_unreadable_queue_fails_the_task(
        self, ingress_mod: ModuleType, world, options,
    ) -> None:
        from scraper.storage_client import StorageAPIError

        world.storage.failing["get_barcode_queue"] = StorageAPIError("down")

        result = ingress_mod._handle_discover()

        assert result["success"] is False

    def test_single_barcode_is_created_and_stocked(
        self, ingress_mod: ModuleType, world, options,
    ) -> None:
        world.kruoka.catalogue = {"K532": {EAN: "Pirkka kevytmaito 1l"}}

        result = ingress_mod._handle_discover({"barcode": EAN})

        assert result["success"] is True
        assert result["product"]["name"] == "Pirkka kevytmaito 1l"
        product_id = world.storage.barcodes[EAN]
        assert result["product_id"] == product_id
        assert world.storage.stock[product_id] == 1.0
        assert result["logs"]


# ---------------------------------------------------------------------------
# _handle_update
# ---------------------------------------------------------------------------


class TestHandleUpdate:
    def test_update_fills_in_placeholder_name_and_availability(
        self, ingress_mod: ModuleType, world, options,
    ) -> None:
        world.kruoka.catalogue = {"K532": {EAN: "Pirkka kevytmaito 1l"}}
        product_id = world.storage.add_product(f"Unknown product ({EAN})", barcode=EAN)

        result = ingress_mod._handle_update()

        assert result["success"] is True
        assert result["logs"]
        assert world.storage.products[product_id]["name"] == "Pirkka kevytmaito 1l"
        assert world.storage.availability[product_id] == [
            {"store_id": "K532", "available": True}
        ]


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

    def test_missing_storage_config(self, ingress_mod: ModuleType) -> None:
        """With no storage_url and no auto-detection, add_products fails."""
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "Test"}]}
            )
        assert result["success"] is False
        assert "Storage URL" in result["error"]

    def test_adds_product_successfully(self, ingress_mod: ModuleType) -> None:
        opts = {
            "storage_url": "http://storage",
        }
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 42

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "scraper.storage_client.StorageClient", return_value=mock_storage
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
        mock_storage.create_product.assert_called_once_with(
            "Maito 1L", description="Kevytmaito", location_id=None, unit_id=None
        )
        mock_storage.add_barcode.assert_called_once_with(42, "6411234000001")

    def test_skips_existing_barcode(self, ingress_mod: ModuleType) -> None:
        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = {"id": 1, "name": "Existing"}

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "scraper.storage_client.StorageClient", return_value=mock_storage
            ):
                result = ingress_mod._handle_add_products(
                    {"products": [{"name": "Existing", "ean": "111"}]}
                )

        assert result["success"] is True
        assert result["added"] == 0
        mock_storage.create_product.assert_not_called()

    def test_handles_storage_error(self, ingress_mod: ModuleType) -> None:
        from scraper.storage_client import StorageAPIError

        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.side_effect = StorageAPIError("conflict")

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "scraper.storage_client.StorageClient", return_value=mock_storage
            ):
                result = ingress_mod._handle_add_products(
                    {"products": [{"name": "Bad", "ean": "222"}]}
                )

        assert result["success"] is False
        assert result["added"] == 0
        assert len(result["errors"]) == 1
        assert "conflict" in result["errors"][0]

    def test_skips_empty_name(self, ingress_mod: ModuleType) -> None:
        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "scraper.storage_client.StorageClient", return_value=mock_storage
            ):
                result = ingress_mod._handle_add_products(
                    {"products": [{"name": "", "ean": "333"}]}
                )

        assert result["added"] == 0
        mock_storage.create_product.assert_not_called()

    def test_forwards_parent_and_group(self, ingress_mod: ModuleType) -> None:
        """HA-storage files a product it finds for a generic item under it."""
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 43
        with mock.patch.object(ingress_mod, "_read_options", return_value={"storage_url": "http://storage"}):
            with mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage):
                result = ingress_mod._handle_add_products({"products": [
                    {"name": "Valio juustokreemi", "ean": "6408430142842",
                     "parent_id": 1941, "product_group_id": 21},
                    {"name": "Toinen", "ean": "6408430142843", "parent_id": "x", "product_group_id": True},
                ]})
        assert result["added_ids"] == [43, 43]
        first, second = mock_storage.create_product.call_args_list
        assert first.kwargs["parent_id"] == 1941 and first.kwargs["product_group_id"] == 21
        assert "parent_id" not in second.kwargs and "product_group_id" not in second.kwargs

    def test_empty_product_list(self, ingress_mod: ModuleType) -> None:
        result = ingress_mod._handle_add_products({"products": []})
        assert result["success"] is False
        assert "No products" in result["error"]

    def test_unexpected_exception(self, ingress_mod: ModuleType) -> None:
        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.side_effect = RuntimeError("unexpected")

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts):
            with mock.patch(
                "scraper.storage_client.StorageClient", return_value=mock_storage
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
            "storage_url": "http://storage",
            "upload_images": True,
        }
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 10

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage), \
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
        assert mock_upload.call_args[0][1] is mock_storage
        assert mock_upload.call_args[0][2] == 10

    def test_skips_image_upload_when_disabled(self, ingress_mod: ModuleType) -> None:
        opts = {
            "storage_url": "http://storage",
            "upload_images": False,
        }
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 10

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage), \
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
            "storage_url": "http://storage",
            "upload_images": True,
        }
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 10

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage), \
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

    def test_collects_and_writes_availability_for_ean(self, ingress_mod: ModuleType) -> None:
        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 77
        entries = [{"store_id": "N110", "available": True}]

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage), \
             mock.patch("main._collect_availability", return_value=entries) as mock_collect, \
             mock.patch("main._write_availability") as mock_write:
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "Maito 1L", "ean": "6411234000001"}]}
            )

        assert result["success"] is True
        assert result["added"] == 1
        mock_collect.assert_called_once_with("6411234000001", [])
        mock_write.assert_called_once_with(mock_storage, 77, entries)

    def test_sweep_price_used_skips_lookup_price(self, ingress_mod: ModuleType) -> None:
        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 5
        entries = [
            {"store_id": "N110", "available": True, "price": 2.5, "price_currency": "EUR"}
        ]

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage), \
             mock.patch("main._make_price_scrapers", return_value=["dummy-scraper"]), \
             mock.patch("main._collect_availability", return_value=entries), \
             mock.patch("main._write_availability"), \
             mock.patch("main._lookup_price") as mock_lookup:
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "Juice", "ean": "999"}]}
            )

        assert result["success"] is True
        mock_storage.create_product.assert_called_once_with(
            "Juice",
            description="",
            location_id=None,
            unit_id=None,
            unit_price=2.5,
            unit_price_currency="EUR",
        )
        mock_lookup.assert_not_called()

    def test_no_ean_skips_availability_sweep(self, ingress_mod: ModuleType) -> None:
        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()
        mock_storage.create_product.return_value = 3

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage), \
             mock.patch("main._collect_availability") as mock_collect, \
             mock.patch("main._write_availability") as mock_write:
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "No EAN item"}]}
            )

        assert result["success"] is True
        assert result["added"] == 1
        mock_collect.assert_not_called()
        mock_write.assert_not_called()

    def test_availability_sweep_and_write_failures_do_not_fail_request(
        self, ingress_mod: ModuleType
    ) -> None:
        opts = {"storage_url": "http://storage"}
        mock_storage = mock.MagicMock()
        mock_storage.get_product_by_barcode.return_value = None
        mock_storage.create_product.return_value = 9

        with mock.patch.object(ingress_mod, "_read_options", return_value=opts), \
             mock.patch("scraper.storage_client.StorageClient", return_value=mock_storage), \
             mock.patch("main._collect_availability", side_effect=RuntimeError("sweep boom")), \
             mock.patch("main._write_availability", side_effect=RuntimeError("write boom")):
            result = ingress_mod._handle_add_products(
                {"products": [{"name": "Flaky", "ean": "123"}]}
            )

        assert result["success"] is True
        assert result["added"] == 1
        assert result["errors"] == []


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

    def _wait_for_task(self, ingress_mod: ModuleType, task_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, task = self._make_handler(ingress_mod, "GET", f"/api/task/{task_id}")
            assert status == 200
            if task["status"] != "running":
                return task
            time.sleep(0.01)
        raise AssertionError(f"task {task_id} still running")

    def test_get_root_serves_html(self, ingress_mod: ModuleType) -> None:
        status, body = self._make_handler(ingress_mod, "GET", "/")
        assert status == 200
        assert "Scraper" in body

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
        """Without a Storage URL the queue cannot be read: the task fails."""
        with mock.patch.object(ingress_mod, "_read_options", return_value={}):
            status, body = self._make_handler(ingress_mod, "POST", "/api/discover")
            assert status == 200
            assert body["status"] == "running"
            result = self._wait_for_task(ingress_mod, body["task_id"])
        assert result["success"] is False

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

    def test_has_update_button(self, ingress_mod: ModuleType) -> None:
        assert 'id="update-btn"' in ingress_mod._HTML

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
