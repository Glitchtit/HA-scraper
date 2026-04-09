"""Unit tests for the Grocy API client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from grocy_scraper.grocy_client import GrocyAPIError, GrocyClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(base_url: str = "https://grocy.example.com", api_key: str = "test") -> tuple[GrocyClient, MagicMock]:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    client = GrocyClient(base_url=base_url, api_key=api_key, session=session)
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
# GrocyClient._url
# ---------------------------------------------------------------------------

class TestUrl:
    def test_trailing_slash_stripped(self):
        client, _ = _make_client(base_url="https://grocy.example.com/")
        assert client._url("/api/objects/products") == "https://grocy.example.com/api/objects/products"

    def test_path_without_leading_slash(self):
        client, _ = _make_client()
        assert client._url("api/objects/products") == "https://grocy.example.com/api/objects/products"


# ---------------------------------------------------------------------------
# GrocyClient.get_product_by_barcode
# ---------------------------------------------------------------------------

class TestGetProductByBarcode:
    def test_found(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(
            json_data={"product": {"id": 1, "name": "Maito"}}
        )
        result = client.get_product_by_barcode("1234567890123")
        assert result == {"id": 1, "name": "Maito"}

    def test_found_bare_product(self):
        """Some Grocy versions return the product at top level."""
        client, session = _make_client()
        session.get.return_value = _mock_response(
            json_data={"id": 2, "name": "Kerma"}
        )
        result = client.get_product_by_barcode("111")
        assert result == {"id": 2, "name": "Kerma"}

    def test_not_found_400(self):
        """A 400 response means the barcode is not registered."""
        client, session = _make_client()
        resp = _mock_response(status_code=400)
        session.get.return_value = resp
        result = client.get_product_by_barcode("999")
        assert result is None

    def test_not_found_404(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=404))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=404)
        result = client.get_product_by_barcode("999")
        assert result is None

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError):
            client.get_product_by_barcode("123")

    def test_connection_error_raises(self):
        client, session = _make_client()
        session.get.side_effect = requests.ConnectionError("refused")
        with pytest.raises(GrocyAPIError):
            client.get_product_by_barcode("123")


# ---------------------------------------------------------------------------
# GrocyClient.create_product
# ---------------------------------------------------------------------------

class TestCreateProduct:
    def test_creates_product_returns_id(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(
            json_data={"created_object_id": 42}
        )
        product_id = client.create_product("Maito", description="Täysmaito")
        assert product_id == 42

    def test_payload_includes_name(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"created_object_id": 1})
        client.create_product("Juusto")
        _, kwargs = session.post.call_args
        assert kwargs["json"]["name"] == "Juusto"

    def test_payload_includes_optional_fields(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"created_object_id": 1})
        client.create_product(
            "Jogurtti",
            description="Mansikkajogurtti",
            location_id=3,
            quantity_unit_id=5,
        )
        _, kwargs = session.post.call_args
        payload = kwargs["json"]
        assert payload["description"] == "Mansikkajogurtti"
        assert payload["location_id"] == 3
        assert payload["qu_id_stock"] == 5
        assert payload["qu_id_purchase"] == 5

    def test_missing_created_id_raises(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"other": "data"})
        with pytest.raises(GrocyAPIError):
            client.create_product("Voi")

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=422))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=422)
        with pytest.raises(GrocyAPIError):
            client.create_product("Voi")


# ---------------------------------------------------------------------------
# GrocyClient.add_barcode
# ---------------------------------------------------------------------------

class TestAddBarcode:
    def test_adds_barcode(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={})
        client.add_barcode(1, "9876543210987")
        _, kwargs = session.post.call_args
        assert kwargs["json"]["product_id"] == 1
        assert kwargs["json"]["barcode"] == "9876543210987"
        assert kwargs["json"]["amount"] == 1.0

    def test_adds_barcode_with_quantity_unit(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={})
        client.add_barcode(2, "111", quantity_unit_id=7)
        _, kwargs = session.post.call_args
        assert kwargs["json"]["qu_id"] == 7

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError):
            client.add_barcode(1, "123")


# ---------------------------------------------------------------------------
# GrocyClient.get_all_products
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
        with pytest.raises(GrocyAPIError):
            client.get_all_products()


# ---------------------------------------------------------------------------
# GrocyClient.get_all_barcodes
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
        with pytest.raises(GrocyAPIError):
            client.get_all_barcodes()


# ---------------------------------------------------------------------------
# GrocyClient.delete_product
# ---------------------------------------------------------------------------

class TestDeleteProduct:
    def test_success(self):
        client, session = _make_client()
        session.delete.return_value = _mock_response()
        client.delete_product(42)
        session.delete.assert_called_once()
        url = session.delete.call_args[0][0]
        assert "/api/objects/products/42" in url

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=404))
        session.delete.return_value = _mock_response(raise_for=http_err, status_code=404)
        with pytest.raises(GrocyAPIError, match="delete product"):
            client.delete_product(42)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.delete.side_effect = requests.ConnectionError("refused")
        with pytest.raises(GrocyAPIError, match="delete product"):
            client.delete_product(42)


# ---------------------------------------------------------------------------
# GrocyClient.upload_product_image
# ---------------------------------------------------------------------------

class TestUploadProductImage:
    def test_uploads_and_sets_picture(self):
        client, session = _make_client()
        session.put.return_value = _mock_response()
        client.upload_product_image(42, "test.jpg", b"\xff\xd8", content_type="image/jpeg")

        assert session.put.call_count == 2
        # First call: file upload
        upload_url = session.put.call_args_list[0][0][0]
        assert "/api/files/productpictures/" in upload_url
        upload_kwargs = session.put.call_args_list[0][1]
        assert upload_kwargs["data"] == b"\xff\xd8"
        assert upload_kwargs["headers"]["Content-Type"] == "image/jpeg"
        # Second call: set picture_file_name
        set_url = session.put.call_args_list[1][0][0]
        assert "/api/objects/products/42" in set_url
        assert session.put.call_args_list[1][1]["json"]["picture_file_name"] == "test.jpg"

    def test_filename_is_base64_encoded_in_url(self):
        import base64
        client, session = _make_client()
        session.put.return_value = _mock_response()
        client.upload_product_image(1, "photo.png", b"\x89PNG")

        upload_url = session.put.call_args_list[0][0][0]
        expected = base64.b64encode(b"photo.png").decode()
        assert expected in upload_url

    def test_upload_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.put.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError, match="upload image"):
            client.upload_product_image(1, "x.jpg", b"data")

    def test_set_picture_http_error_raises(self):
        client, session = _make_client()
        ok_resp = _mock_response()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        err_resp = _mock_response(raise_for=http_err, status_code=500)
        session.put.side_effect = [ok_resp, err_resp]
        with pytest.raises(GrocyAPIError, match="set picture"):
            client.upload_product_image(1, "x.jpg", b"data")


# ---------------------------------------------------------------------------
# get_locations
# ---------------------------------------------------------------------------

class TestGetLocations:
    def test_returns_locations_list(self):
        client, session = _make_client()
        locations = [{"id": 2, "name": "Fridge"}, {"id": 4, "name": "Cupboard"}]
        session.get.return_value = _mock_response(locations)
        result = client.get_locations()
        assert result == locations
        url = session.get.call_args[0][0]
        assert "/api/objects/locations" in url

    def test_returns_empty_list_on_null_response(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(None)
        assert client.get_locations() == []

    def test_request_error_raises(self):
        client, session = _make_client()
        session.get.side_effect = requests.RequestException("timeout")
        with pytest.raises(GrocyAPIError, match="locations"):
            client.get_locations()


# ---------------------------------------------------------------------------
# ensure_product_group
# ---------------------------------------------------------------------------

class TestEnsureProductGroup:
    def test_returns_existing_group_id(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(
            json_data=[{"id": 7, "name": "Group master"}]
        )
        result = client.ensure_product_group("Group master")
        assert result == 7
        # Should only GET, no POST.
        session.post.assert_not_called()

    def test_creates_group_when_missing(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(json_data=[])
        session.post.return_value = _mock_response(
            json_data={"created_object_id": 15}
        )
        result = client.ensure_product_group("Group master")
        assert result == 15
        _, kwargs = session.post.call_args
        assert kwargs["json"]["name"] == "Group master"

    def test_get_error_raises(self):
        client, session = _make_client()
        session.get.side_effect = requests.RequestException("timeout")
        with pytest.raises(GrocyAPIError, match="product groups"):
            client.ensure_product_group("Group master")

    def test_create_http_error_raises(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(json_data=[])
        http_err = requests.HTTPError(response=MagicMock(status_code=422, text=""))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=422)
        with pytest.raises(GrocyAPIError, match="create product group"):
            client.ensure_product_group("Group master")


# ---------------------------------------------------------------------------
# update_product
# ---------------------------------------------------------------------------

class TestUpdateProduct:
    def test_sends_put_with_fields(self):
        client, session = _make_client()
        session.put.return_value = _mock_response()
        client.update_product(7, location_id=3, default_best_before_days=30)
        url = session.put.call_args[0][0]
        assert "/api/objects/products/7" in url
        body = session.put.call_args[1]["json"]
        assert body == {"location_id": 3, "default_best_before_days": 30}

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=422, text=""))
        session.put.return_value = _mock_response(raise_for=http_err, status_code=422)
        with pytest.raises(GrocyAPIError, match="update product"):
            client.update_product(7, location_id=99)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.put.side_effect = requests.RequestException("connection refused")
        with pytest.raises(GrocyAPIError, match="update product"):
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
        assert "/api/stock/products/42/add" in url
        body = session.post.call_args[1]["json"]
        assert body["amount"] == 2.0
        assert body["transaction_type"] == "purchase"

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
        with pytest.raises(GrocyAPIError, match="add stock"):
            client.add_stock(42)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.post.side_effect = requests.RequestException("timeout")
        with pytest.raises(GrocyAPIError, match="add stock"):
            client.add_stock(42)


# ---------------------------------------------------------------------------
# get_product_stock_locations
# ---------------------------------------------------------------------------

class TestGetProductStockLocations:
    def test_returns_list(self):
        client, session = _make_client()
        data = [
            {"location_id": 2, "amount": 3.0},
            {"location_id": 5, "amount": 1.0},
        ]
        session.get.return_value = _mock_response(json_data=data)
        result = client.get_product_stock_locations(42)
        assert result == data
        url = session.get.call_args[0][0]
        assert "/api/stock/products/42/locations" in url

    def test_returns_empty_list_on_null(self):
        client, session = _make_client()
        session.get.return_value = _mock_response(json_data=None)
        assert client.get_product_stock_locations(1) == []

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError, match="stock locations"):
            client.get_product_stock_locations(42)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.get.side_effect = requests.RequestException("timeout")
        with pytest.raises(GrocyAPIError, match="stock locations"):
            client.get_product_stock_locations(42)


# ---------------------------------------------------------------------------
# transfer_stock
# ---------------------------------------------------------------------------

class TestTransferStock:
    def test_success(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={})
        client.transfer_stock(42, amount=3.0, location_id_from=2, location_id_to=5)
        session.post.assert_called_once()
        url = session.post.call_args[0][0]
        assert "/api/stock/products/42/transfer" in url
        body = session.post.call_args[1]["json"]
        assert body["amount"] == 3.0
        assert body["location_id_from"] == 2
        assert body["location_id_to"] == 5

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=400, text=""))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=400)
        with pytest.raises(GrocyAPIError, match="transfer stock"):
            client.transfer_stock(42, 1.0, 2, 5)

    def test_request_error_raises(self):
        client, session = _make_client()
        session.post.side_effect = requests.RequestException("timeout")
        with pytest.raises(GrocyAPIError, match="transfer stock"):
            client.transfer_stock(42, 1.0, 2, 5)


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
        assert "/api/objects/quantity_units" in session.get.call_args[0][0]

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500, text=""))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError, match="quantity units"):
            client.get_quantity_units()


class TestGetQuantityUnitConversions:
    def test_returns_conversions(self):
        client, session = _make_client()
        convs = [{"id": 1, "from_qu_id": 2, "to_qu_id": 3, "factor": 1000}]
        session.get.return_value = _mock_response(json_data=convs)
        result = client.get_quantity_unit_conversions()
        assert result == convs

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500, text=""))
        session.get.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError, match="QU conversions"):
            client.get_quantity_unit_conversions()


class TestCreateQuantityUnit:
    def test_creates_unit(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"created_object_id": 5})
        uid = client.create_quantity_unit("Gramma", "Grammaa", "g")
        assert uid == 5
        payload = session.post.call_args[1]["json"]
        assert payload["name"] == "Gramma"
        assert payload["name_plural"] == "Grammaa"
        assert payload["description"] == "g"

    def test_minimal_payload(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"created_object_id": 6})
        uid = client.create_quantity_unit("Litra")
        assert uid == 6
        payload = session.post.call_args[1]["json"]
        assert payload == {"name": "Litra"}

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=400, text=""))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=400)
        with pytest.raises(GrocyAPIError, match="create QU"):
            client.create_quantity_unit("Bad")


class TestCreateQuantityUnitConversion:
    def test_creates_global_conversion(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"created_object_id": 10})
        cid = client.create_quantity_unit_conversion(2, 3, 1000.0)
        assert cid == 10
        payload = session.post.call_args[1]["json"]
        assert payload == {"from_qu_id": 2, "to_qu_id": 3, "factor": 1000.0}

    def test_creates_product_specific_conversion(self):
        client, session = _make_client()
        session.post.return_value = _mock_response(json_data={"created_object_id": 11})
        cid = client.create_quantity_unit_conversion(1, 5, 1.0, product_id=42)
        assert cid == 11
        payload = session.post.call_args[1]["json"]
        assert payload["product_id"] == 42

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=400, text=""))
        session.post.return_value = _mock_response(raise_for=http_err, status_code=400)
        with pytest.raises(GrocyAPIError, match="QU conversion"):
            client.create_quantity_unit_conversion(1, 2, 1.0)


class TestDeleteQuantityUnitConversion:
    def test_deletes_conversion(self):
        client, session = _make_client()
        session.delete.return_value = _mock_response(status_code=204)
        client.delete_quantity_unit_conversion(10)
        assert "/api/objects/quantity_unit_conversions/10" in session.delete.call_args[0][0]

    def test_404_is_silent(self):
        client, session = _make_client()
        session.delete.return_value = _mock_response(status_code=404)
        client.delete_quantity_unit_conversion(99)  # Should not raise

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500, text=""))
        session.delete.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError, match="delete QU conversion"):
            client.delete_quantity_unit_conversion(10)


class TestDeleteQuantityUnit:
    def test_deletes_unit(self):
        client, session = _make_client()
        session.delete.return_value = _mock_response(status_code=204)
        client.delete_quantity_unit(5)
        assert "/api/objects/quantity_units/5" in session.delete.call_args[0][0]

    def test_404_is_silent(self):
        client, session = _make_client()
        session.delete.return_value = _mock_response(status_code=404)
        client.delete_quantity_unit(99)  # Should not raise

    def test_http_error_raises(self):
        client, session = _make_client()
        http_err = requests.HTTPError(response=MagicMock(status_code=500, text=""))
        session.delete.return_value = _mock_response(raise_for=http_err, status_code=500)
        with pytest.raises(GrocyAPIError, match="delete QU"):
            client.delete_quantity_unit(5)
