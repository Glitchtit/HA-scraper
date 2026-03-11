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
