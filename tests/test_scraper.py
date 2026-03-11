"""Unit tests for the k-ruoka.fi scraper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from grocy_scraper.scraper import KRuokaScraper, Product


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(responses: list[dict]) -> MagicMock:
    """Return a mock requests.Session that returns *responses* in order."""
    session = MagicMock(spec=requests.Session)
    session.headers = {}

    mock_responses = []
    for payload in responses:
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status.return_value = None
        mock_responses.append(mock_resp)

    session.get.side_effect = mock_responses
    return session


# ---------------------------------------------------------------------------
# Product._parse_product
# ---------------------------------------------------------------------------

class TestParseProduct:
    def test_standard_keys(self):
        item = {"name": "Maito", "ean": "1234567890123", "id": "42"}
        product = KRuokaScraper._parse_product(item)
        assert product is not None
        assert product.name == "Maito"
        assert product.ean == "1234567890123"
        assert product.product_id == "42"

    def test_alternate_ean_keys(self):
        for key in ("EAN", "barcode", "eanCode", "gtin"):
            item = {"name": "Juusto", key: "9876543210987"}
            product = KRuokaScraper._parse_product(item)
            assert product is not None, f"Failed for key {key!r}"
            assert product.ean == "9876543210987"

    def test_alternate_name_keys(self):
        for key in ("title", "productName"):
            item = {key: "Leipä", "ean": "111"}
            product = KRuokaScraper._parse_product(item)
            assert product is not None, f"Failed for key {key!r}"
            assert product.name == "Leipä"

    def test_missing_name_and_ean_returns_none(self):
        product = KRuokaScraper._parse_product({"id": "99"})
        assert product is None

    def test_strips_whitespace(self):
        item = {"name": "  Voi  ", "ean": "  123  "}
        product = KRuokaScraper._parse_product(item)
        assert product is not None
        assert product.name == "Voi"
        assert product.ean == "123"

    def test_description_and_image(self):
        item = {
            "name": "Keksit",
            "ean": "555",
            "description": "Herkullisia",
            "imageUrl": "https://example.com/img.jpg",
        }
        product = KRuokaScraper._parse_product(item)
        assert product is not None
        assert product.description == "Herkullisia"
        assert product.image_url == "https://example.com/img.jpg"


# ---------------------------------------------------------------------------
# KRuokaScraper._extract_items
# ---------------------------------------------------------------------------

class TestExtractItems:
    def test_products_key(self):
        data = {"products": [{"name": "A"}, {"name": "B"}], "total": 2}
        assert KRuokaScraper._extract_items(data) == [{"name": "A"}, {"name": "B"}]

    def test_items_key(self):
        data = {"items": [{"name": "C"}]}
        assert KRuokaScraper._extract_items(data) == [{"name": "C"}]

    def test_results_key(self):
        data = {"results": [{"name": "D"}]}
        assert KRuokaScraper._extract_items(data) == [{"name": "D"}]

    def test_data_key(self):
        data = {"data": [{"name": "E"}]}
        assert KRuokaScraper._extract_items(data) == [{"name": "E"}]

    def test_bare_list(self):
        data = [{"name": "F"}]
        assert KRuokaScraper._extract_items(data) == [{"name": "F"}]  # type: ignore[arg-type]

    def test_empty_on_unknown_shape(self):
        assert KRuokaScraper._extract_items({"unknown": "value"}) == []


# ---------------------------------------------------------------------------
# KRuokaScraper._extract_total
# ---------------------------------------------------------------------------

class TestExtractTotal:
    def test_total_key(self):
        assert KRuokaScraper._extract_total({"total": 100}) == 100

    def test_totalcount_key(self):
        assert KRuokaScraper._extract_total({"totalCount": 50}) == 50

    def test_count_key(self):
        assert KRuokaScraper._extract_total({"count": 10}) == 10

    def test_missing_returns_none(self):
        assert KRuokaScraper._extract_total({"products": []}) is None


# ---------------------------------------------------------------------------
# KRuokaScraper.search – pagination
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_single_page(self):
        """A response with fewer items than page size stops after one page."""
        page = {
            "products": [
                {"name": "Maito", "ean": "111"},
                {"name": "Kerma", "ean": "222"},
            ],
            "total": 2,
        }
        session = _make_session([page])
        scraper = KRuokaScraper(store_id="P048", session=session, request_delay=0)
        products = list(scraper.search("maito"))

        assert len(products) == 2
        assert products[0].name == "Maito"
        assert products[1].ean == "222"
        assert session.get.call_count == 1

    def test_search_passes_query_param(self):
        """The query string is forwarded to the API."""
        session = _make_session([{"products": [], "total": 0}])
        scraper = KRuokaScraper(store_id="P048", session=session, request_delay=0)
        list(scraper.search("leipä"))

        _, kwargs = session.get.call_args
        params = kwargs.get("params", {})
        assert params.get("query") == "leipä"
        assert params.get("store") == "P048"

    def test_search_pagination(self):
        """Multiple pages are fetched until total is reached."""
        page1 = {
            "products": [{"name": f"P{i}", "ean": str(i)} for i in range(24)],
            "total": 30,
        }
        page2 = {
            "products": [{"name": f"P{i}", "ean": str(i)} for i in range(24, 30)],
            "total": 30,
        }
        session = _make_session([page1, page2])
        scraper = KRuokaScraper(store_id="P048", session=session, request_delay=0)
        products = list(scraper.search("all"))

        assert len(products) == 30
        assert session.get.call_count == 2

    def test_search_max_products(self):
        """max_products limits the number of products returned."""
        page = {
            "products": [{"name": f"P{i}", "ean": str(i)} for i in range(24)],
            "total": 100,
        }
        session = _make_session([page])
        scraper = KRuokaScraper(store_id="P048", session=session, request_delay=0)
        products = list(scraper.search("x", max_products=5))

        assert len(products) == 5

    def test_search_stops_on_empty_page(self):
        """Stops pagination when the API returns an empty page."""
        page1 = {
            "products": [{"name": "A", "ean": "1"}],
            "total": 10,  # claims 10 but sends 1 then nothing
        }
        page2 = {"products": []}
        session = _make_session([page1, page2])
        scraper = KRuokaScraper(store_id="P048", session=session, request_delay=0)
        products = list(scraper.search("x"))

        assert len(products) == 1

    def test_search_handles_http_error(self):
        """HTTP errors are logged and iteration stops gracefully."""
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=500)
        )
        session.get.return_value = mock_resp

        scraper = KRuokaScraper(store_id="P048", session=session, request_delay=0)
        products = list(scraper.search("x"))
        assert products == []


# ---------------------------------------------------------------------------
# KRuokaScraper.browse
# ---------------------------------------------------------------------------

class TestBrowse:
    def test_browse_no_extra_query_param(self):
        """Browse should not pass a 'query' parameter."""
        session = _make_session([{"products": [], "total": 0}])
        scraper = KRuokaScraper(store_id="P048", session=session, request_delay=0)
        list(scraper.browse())

        _, kwargs = session.get.call_args
        params = kwargs.get("params", {})
        assert "query" not in params
