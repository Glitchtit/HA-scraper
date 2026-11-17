"""Unit tests for the Storage API client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from scraper.storage_client import StorageAPIError, StorageClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(base_url: str = "https://storage.example.com") -> tuple[StorageClient, MagicMock]:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    client = StorageClient(base_url=base_url)
    client._session = session
    return client, session


def _mock_response(json_data=None, status_code: int = 200, raise_for: Exception | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if raise_for:
        resp.raise_for_status.side_effect = raise_for
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# StorageClient._url
# ---------------------------------------------------------------------------

class TestUrl:
    def test_trailing_slash_stripped(self):
        client, _ = _make_client(base_url="https://storage.example.com/")
        assert client._url("/api/products") == "https://storage.example.com/api/products"

    def test_path_without_leading_slash(self):
        client, _ = _make_client()
        assert client._url("api/products") == "https://storage.example.com/api/products"


# ---------------------------------------------------------------------------
# StorageClient.get_product_by_barcode
# ---------------------------------------------------------------------------

class TestGetProductByBarcode:
    def test_found(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(
            json_data={"id": 1, "name": "Maito"}
        )
        result = client.get_product_by_barcode("1234567890123")
        assert result == {"id": 1, "name": "Maito"}

    def test_not_found_direct_404(self):
        """A 404 status code (before raise_for_status) returns None."""
        client, session = _make_client()
        resp = _mock_response(status_code=404)
        session.get.return_value = resp
        result = client.get_product_by_barcode("999")
        assert result is None

    def test_not_found_404_http_error(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=404))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=404)
        result = client.get_product_by_barcode("999")
        assert result is None

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(StorageAPIError):
            client.get_product_by_barcode("123")

    def test_connection_error_raises(self):
        client, session = _make_client()
        session.get.side_effect = requests.ConnectionError("refused")
        with pytest.raises(StorageAPIError):
            client.get_product_by_barcode("123")


# ---------------------------------------------------------------------------
# StorageClient.create_product
# ---------------------------------------------------------------------------

class TestCreateProduct:
    def test_creates_product_returns_id(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(
            json_data={"id": 42}
        )
        product_id = client.create_product("Maito", description="Täysmaito", unit_id=1)
        assert product_id == 42

    def test_payload_includes_name(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"id": 1})
        client.create_product("Juusto", unit_id=1)
        _, kwargs = session.post.call_args
        assert kwargs["json"]["name"] == "Juusto"

    def test_payload_includes_optional_fields(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"id": 1})
        client.create_product(
            "Jogurtti",
            description="Mansikkajogurtti",
            location_id=3,
            unit_id=5,
        )
        _, kwargs = session.post.call_args
        payload = kwargs["json"]
        assert payload["description"] == "Mansikkajogurtti"
        assert payload["location_id"] == 3
        assert payload["unit_id"] == 5

    def test_missing_unit_id_auto_detects(self):
        """create_product() no longer raises on unit_id=None; it auto-detects."""
        client, session = _make_client()
        # Mock get_quantity_units to return a unit with abbreviation "kpl"
        session.get.return_value = _mock_response(json_data=[
            {"id": 9, "name": "kappale", "abbreviation": "kpl"}
        ])
        session.post.return_value = _mock_response(json_data={"id": 42})
        product_id = client.create_product("Voi")
        assert product_id == 42
        # Verify the auto-detected unit was used
        call_kwargs = session.post.call_args
        payload = call_kwargs[1].get("json") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1]["json"]
        assert payload["unit_id"] == 9

    def test_missing_id_in_response_raises(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"other": "data"})
        with pytest.raises(StorageAPIError):
            client.create_product("Voi", unit_id=1)

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=422))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=422)
        with pytest.raises(StorageAPIError):
            client.create_product("Voi", unit_id=1)


# ---------------------------------------------------------------------------
# StorageClient.add_barcode
# ---------------------------------------------------------------------------

class TestAddBarcode:
    def test_adds_barcode(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={})
        client.add_barcode(1, "9876543210987")
        _, kwargs = session.post.call_args
        assert kwargs["json"]["product_id"] == 1
        assert kwargs["json"]["barcode"] == "9876543210987"
        assert kwargs["json"]["pack_size"] == 1.0

    def test_adds_barcode_with_pack_unit(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={})
        client.add_barcode(2, "111", pack_unit_id=7)
        _, kwargs = session.post.call_args
        assert kwargs["json"]["pack_unit_id"] == 7

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(StorageAPIError):
            client.add_barcode(1, "123")


# ---------------------------------------------------------------------------
# StorageClient.get_all_products
# ---------------------------------------------------------------------------

class TestGetAllProducts:
    def test_returns_list(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(json_data=[{"id": 1}, {"id": 2}])
        products = client.get_all_products()
        assert len(products) == 2

    def test_returns_empty_on_null(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(json_data=None)
        products = client.get_all_products()
        assert products == []

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(StorageAPIError):
            client.get_all_products()


# ---------------------------------------------------------------------------
# StorageClient.get_all_barcodes
# ---------------------------------------------------------------------------

class TestGetAllBarcodes:
    def test_returns_list(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(
            json_data=[{"barcode": "111", "product_id": 1}]
        )
        barcodes = client.get_all_barcodes()
        assert barcodes[0]["barcode"] == "111"

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(StorageAPIError):
            client.get_all_barcodes()


# ---------------------------------------------------------------------------
# StorageClient.delete_product
# ---------------------------------------------------------------------------

class TestDeleteProduct:
    def test_success(self):
        client, session = _make_client()
        session.delete.return_value = _mock_response()
        client.delete_product(42)
        session.delete.assert_called_once()
        url = session.delete.call_args[0][0]
        assert "/api/products/42" in url

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=404))
        session.delete.return_value = _mock_response(raise_for=http_err, status_code=404)
        with pytest.raises(StorageAPIError, match="delete product"):
            client.delete_product(42)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.delete.side_effect = requests.ConnectionError("refused")
        with pytest.raises(StorageAPIError, match="delete product"):
            client.delete_product(42)


# ---------------------------------------------------------------------------
# StorageClient.upload_product_image
# ---------------------------------------------------------------------------

class TestUploadProductImage:
    def test_uploads_and_sets_picture(self):
        client, session = _make_client()
        session.put.return_value = _mock_response()
        client.upload_product_image(42, "test.jpg", b"\xff\xd8", content_type="image/jpeg")

        assert session.put.call_count == 2
        # First call: file upload
        upload_url = session.put.call_args_list[0][0][0]
        assert "/api/files/products/test.jpg" in upload_url
        upload_kwargs = session.put.call_args_list[0][1]
        assert upload_kwargs["data"] == b"\xff\xd8"
        assert upload_kwargs["headers"]["Content-Type"] == "image/jpeg"
        # Second call: set picture_filename
        set_url = session.put.call_args_list[1][0][0]
        assert "/api/products/42" in set_url
        assert session.put.call_args_list[1][1]["json"]["picture_filename"] == "test.jpg"

    def test_filename_in_url_is_plain(self):
        """Storage API uses plain filenames in the URL (not Base64 encoded)."""
        client, session = _make_client()
        session.put.return_value = _mock_response()
        client.upload_product_image(1, "photo.png", b"\x89PNG")

        upload_url = session.put.call_args_list[0][0][0]
        assert "photo.png" in upload_url

    def test_upload_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.put.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(StorageAPIError, match="upload image"):
            client.upload_product_image(1, "x.jpg", b"data")

    def test_set_picture_http_error_raises(self):
        client, session = _make_client()
        ok_resp = _mock_response()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        err_resp = _mock_response(raise_for=http_err, status_code=500)
        session.put.side_effect = [ok_resp, err_resp]
        with pytest.raises(StorageAPIError, match="set picture"):
            client.upload_product_image(1, "x.jpg", b"data")


# ---------------------------------------------------------------------------
# get_locations
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ensure_product_group
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# update_product
# ---------------------------------------------------------------------------

class TestUpdateProduct:
    def test_sends_put_with_fields(self):
        client, session = _make_client()
        session.put.return_value = _mock_response()
        client.update_product(7, location_id=3, default_best_before_days=30)
        url = session.put.call_args[0][0]
        assert "/api/products/7" in url
        body = session.put.call_args[1]["json"]
        assert body == {"location_id": 3, "default_best_before_days": 30}

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=422, text=""))
        session.put.return_value = _mock_response(raise_for=http_err, status_code=422)
        with pytest.raises(StorageAPIError, match="update product"):
            client.update_product(7, location_id=99)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.put.side_effect = requests.RequestException("connection refused")
        with pytest.raises(StorageAPIError, match="update product"):
            client.update_product(5, location_id=2)


# ---------------------------------------------------------------------------
# add_stock
# ---------------------------------------------------------------------------

class TestAddStock:
    def test_success(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={})
        client.add_stock(42, amount=2.0)
        session.post.assert_called_once()
        url = session.post.call_args[0][0]
        assert "/api/stock/add" in url
        body = session.post.call_args[1]["json"]
        assert body["product_id"] == 42
        assert body["amount"] == 2.0

    def test_default_amount_is_one(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={})
        client.add_stock(10)
        body = session.post.call_args[1]["json"]
        assert body["amount"] == 1.0

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=400, text=""))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=400)
        with pytest.raises(StorageAPIError, match="add stock"):
            client.add_stock(42)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.post.side_effect = requests.RequestException("timeout")
        with pytest.raises(StorageAPIError, match="add stock"):
            client.add_stock(42)


# ---------------------------------------------------------------------------
# get_product_stock_locations
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# transfer_stock
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Quantity units & conversions
# ---------------------------------------------------------------------------


class TestGetQuantityUnits:
    def test_returns_units(self):
        client, session = _make_client()
        units = [{"id": 1, "name": "Piece"}, {"id": 2, "name": "Gramma"}]
        session.get.return_value = _mock_response(json_data=units)
        result = client.get_quantity_units()
        assert result == units
        assert "/api/units" in session.get.call_args[0][0]

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500, text=""))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(StorageAPIError, match="quantity units"):
            client.get_quantity_units()


# ---------------------------------------------------------------------------
# Stores / availability
# ---------------------------------------------------------------------------

class TestUpsertStore:
    def test_puts_to_store_endpoint(self):
        client, session = _make_client()
        session.put.return_value = _mock_response({"id": "N110", "name": "K-Citymarket Kupittaa"})
        client.upsert_store("N110", "K-Citymarket Kupittaa")
        session.put.assert_called_once()
        url = session.put.call_args[0][0]
        assert url == "https://storage.example.com/api/stores/N110"
        assert session.put.call_args.kwargs["json"] == {"name": "K-Citymarket Kupittaa"}

    def test_http_error_raises_storage_api_error(self):
        client, session = _make_client()
        err = requests.HTTPError(response=_mock_response({"detail": "boom"}, 500))
        session.put.return_value = _mock_response(raise_for=err)
        with pytest.raises(StorageAPIError):
            client.upsert_store("N110", "X")


class TestSetProductAvailability:
    def test_puts_entries_as_body(self):
        client, session = _make_client()
        session.put.return_value = _mock_response([])
        entries = [{"store_id": "N110", "available": True, "price": 2.35,
                    "price_currency": "EUR"},
                   {"store_id": "K532", "available": False}]
        client.set_product_availability(42, entries)
        url = session.put.call_args[0][0]
        assert url == "https://storage.example.com/api/products/42/availability"
        assert session.put.call_args.kwargs["json"] == entries

    def test_request_error_raises_storage_api_error(self):
        client, session = _make_client()
        session.put.side_effect = requests.ConnectionError("down")
        with pytest.raises(StorageAPIError):
            client.set_product_availability(42, [{"store_id": "N110", "available": True}])
