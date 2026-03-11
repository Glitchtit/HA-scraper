"""Unit tests for the k-ruoka.fi scraper."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
import requests

from grocy_scraper.scraper import KRuokaScraper, Product


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(responses: list[dict | Exception]) -> MagicMock:
    """Return a mock session whose .post() returns *responses* in order."""
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    session.cookies = MagicMock()
    session.get = MagicMock()  # ensure .get exists for "not called" assertions

    side_effects = []
    for payload in responses:
        if isinstance(payload, Exception):
            side_effects.append(payload)
        else:
            mock_resp = MagicMock()
            mock_resp.json.return_value = payload
            mock_resp.raise_for_status.return_value = None
            side_effects.append(mock_resp)

    session.post.side_effect = side_effects
    return session


def _search_page(items: list[dict], total: int) -> dict:
    """Build a product-search API response."""
    return {"results": items, "totalHits": total}


def _offer_categories_response(slugs: list[str]) -> dict:
    """Build an offer-categories API response."""
    return {
        "offerCategories": [
            {"slug": s, "count": 1, "name": {"finnish": s}} for s in slugs
        ]
    }


def _offer_category_response(offers: list[dict], total: int) -> dict:
    """Build an offer-category API response."""
    return {"offers": offers, "totalHits": total}


def _make_offer(name: str, ean: str) -> dict:
    """Build a minimal offer item as returned by /offer-category."""
    return {
        "id": f"S{ean}",
        "product": {
            "id": ean,
            "product": {
                "ean": ean,
                "localizedName": {"finnish": name},
                "images": [],
                "productAttributes": {"ean": ean},
            },
        },
    }


def _make_search_item(name: str, ean: str) -> dict:
    """Build a minimal product item as returned by /v2/product-search."""
    return {"id": ean, "ean": ean, "localizedName": {"finnish": name}}


# ---------------------------------------------------------------------------
# _parse_search_product
# ---------------------------------------------------------------------------

class TestParseSearchProduct:
    def test_standard_keys(self):
        item = _make_search_item("Maito", "1234567890123")
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.name == "Maito"
        assert p.ean == "1234567890123"
        assert p.product_id == "1234567890123"

    def test_localized_name_preferred(self):
        """localizedName.finnish should take precedence over flat 'name'."""
        item = {
            "name": "En",
            "ean": "111",
            "localizedName": {"finnish": "Fi"},
        }
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.name == "Fi"

    def test_fallback_to_flat_name(self):
        """Falls back to 'name' when localizedName is absent."""
        item = {"name": "Tuote", "ean": "222"}
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.name == "Tuote"

    def test_alternate_ean_keys(self):
        for key in ("EAN", "barcode", "eanCode", "gtin"):
            item = {"name": "Juusto", key: "9876543210987"}
            p = KRuokaScraper._parse_search_product(item)
            assert p is not None, f"Failed for key {key!r}"
            assert p.ean == "9876543210987"

    def test_missing_name_and_ean_returns_none(self):
        assert KRuokaScraper._parse_search_product({"id": "99"}) is None

    def test_strips_whitespace(self):
        item = {"name": "  Voi  ", "ean": "  123  "}
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.name == "Voi"
        assert p.ean == "123"

    def test_image_url_extracted(self):
        item = {"name": "Keksit", "ean": "555", "imageUrl": "https://example.com/img.jpg"}
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.image_url == "https://example.com/img.jpg"

    def test_images_list_fallback(self):
        """Falls back to the first element of the images list."""
        item = {"name": "Pasta", "ean": "777", "images": ["https://example.com/p.jpg"]}
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.image_url == "https://example.com/p.jpg"


# ---------------------------------------------------------------------------
# _parse_offer_product
# ---------------------------------------------------------------------------

class TestParseOfferProduct:
    def test_standard_offer_shape(self):
        offer = _make_offer("Porkkanasose", "6418248002382")
        p = KRuokaScraper._parse_offer_product(offer)
        assert p is not None
        assert p.name == "Porkkanasose"
        assert p.ean == "6418248002382"

    def test_ean_from_product_attributes(self):
        """Falls back to productAttributes.ean if top-level ean is missing."""
        offer = {
            "id": "SX",
            "product": {
                "product": {
                    "localizedName": {"finnish": "Tuote"},
                    "productAttributes": {"ean": "999"},
                }
            },
        }
        p = KRuokaScraper._parse_offer_product(offer)
        assert p is not None
        assert p.ean == "999"

    def test_name_from_offer_title_fallback(self):
        """Falls back to offer.title when localizedName is missing."""
        offer = {
            "id": "SX",
            "title": "Banaani",
            "product": {"product": {"ean": "888"}},
        }
        p = KRuokaScraper._parse_offer_product(offer)
        assert p is not None
        assert p.name == "Banaani"

    def test_missing_name_and_ean_returns_none(self):
        assert KRuokaScraper._parse_offer_product({}) is None

    def test_product_id_from_inner(self):
        offer = _make_offer("Kerma", "100")
        p = KRuokaScraper._parse_offer_product(offer)
        assert p is not None
        assert p.product_id == "100"


# ---------------------------------------------------------------------------
# _extract_search_results
# ---------------------------------------------------------------------------

class TestExtractSearchResults:
    def test_results_key(self):
        data = {"results": [{"ean": "1"}, {"ean": "2"}], "totalHits": 2}
        assert KRuokaScraper._extract_search_results(data) == [{"ean": "1"}, {"ean": "2"}]

    def test_products_key(self):
        data = {"products": [{"ean": "A"}]}
        assert KRuokaScraper._extract_search_results(data) == [{"ean": "A"}]

    def test_items_key(self):
        data = {"items": [{"ean": "B"}]}
        assert KRuokaScraper._extract_search_results(data) == [{"ean": "B"}]

    def test_data_key(self):
        data = {"data": [{"ean": "C"}]}
        assert KRuokaScraper._extract_search_results(data) == [{"ean": "C"}]

    def test_bare_list(self):
        data = [{"ean": "D"}]
        assert KRuokaScraper._extract_search_results(data) == [{"ean": "D"}]  # type: ignore[arg-type]

    def test_empty_on_unknown_shape(self):
        assert KRuokaScraper._extract_search_results({"unknown": "val"}) == []


# ---------------------------------------------------------------------------
# _extract_total
# ---------------------------------------------------------------------------

class TestExtractTotal:
    def test_totalHits_key(self):
        assert KRuokaScraper._extract_total({"totalHits": 100}) == 100

    def test_total_key(self):
        assert KRuokaScraper._extract_total({"total": 50}) == 50

    def test_totalcount_key(self):
        assert KRuokaScraper._extract_total({"totalCount": 30}) == 30

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
        items = [_make_search_item("Maito", "111"), _make_search_item("Kerma", "222")]
        page = _search_page(items, total=2)
        session = _make_session([page])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("maito"))

        assert len(products) == 2
        assert products[0].name == "Maito"
        assert products[1].ean == "222"
        assert session.post.call_count == 1

    def test_search_uses_post(self):
        """search() uses POST, not GET."""
        session = _make_session([_search_page([], 0)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("maito"))

        assert session.post.called
        assert not session.get.called

    def test_search_url_contains_query(self):
        """The query is embedded in the URL path."""
        session = _make_session([_search_page([], 0)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("maito"))

        call_url = session.post.call_args[0][0]
        assert "maito" in call_url

    def test_search_url_contains_store_id(self):
        """storeId is passed in the query string."""
        session = _make_session([_search_page([], 0)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("maito"))

        call_url = session.post.call_args[0][0]
        assert "storeId=N110" in call_url

    def test_search_pagination(self):
        """Multiple pages are fetched when totalHits > page size."""
        p1 = _search_page(
            [_make_search_item(f"P{i}", str(i)) for i in range(100)], total=110
        )
        p2 = _search_page(
            [_make_search_item(f"P{i}", str(i)) for i in range(100, 110)], total=110
        )
        session = _make_session([p1, p2])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("all"))

        assert len(products) == 110
        assert session.post.call_count == 2

    def test_search_max_products(self):
        """max_products limits the number of products returned."""
        page = _search_page(
            [_make_search_item(f"P{i}", str(i)) for i in range(100)], total=500
        )
        session = _make_session([page])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("x", max_products=5))

        assert len(products) == 5

    def test_search_stops_on_empty_page(self):
        """Stops pagination when the API returns an empty results list."""
        p1 = _search_page([_make_search_item("A", "1")], total=10)
        p2 = _search_page([], total=10)
        session = _make_session([p1, p2])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("x"))

        assert len(products) == 1

    def test_search_handles_http_error(self):
        """HTTP errors are logged and iteration stops gracefully."""
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.cookies = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=500)
        )
        session.post.return_value = mock_resp

        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("x"))
        assert products == []


# ---------------------------------------------------------------------------
# KRuokaScraper.browse
# ---------------------------------------------------------------------------

class TestBrowse:
    def test_browse_fetches_categories_first(self):
        """browse() first calls offer-categories, then offer-category."""
        cats = _offer_categories_response(["juomat"])
        offers = _offer_category_response([_make_offer("Kalja", "333")], total=1)
        session = _make_session([cats, offers])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse())

        assert len(products) == 1
        assert products[0].name == "Kalja"
        assert products[0].ean == "333"
        assert session.post.call_count == 2

    def test_browse_iterates_multiple_categories(self):
        """browse() iterates each category slug returned."""
        cats = _offer_categories_response(["juomat", "maito-juusto-munat-ja-rasvat"])
        offers1 = _offer_category_response([_make_offer("Kalja", "1")], total=1)
        offers2 = _offer_category_response([_make_offer("Maito", "2")], total=1)
        session = _make_session([cats, offers1, offers2])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse())

        assert len(products) == 2
        assert {p.ean for p in products} == {"1", "2"}

    def test_browse_paginates_category(self):
        """browse() pages through categories with more than 25 offers."""
        cats = _offer_categories_response(["juomat"])
        # First page: 25 offers, total 30 → triggers second page
        page1 = _offer_category_response(
            [_make_offer(f"X{i}", str(i)) for i in range(25)], total=30
        )
        page2 = _offer_category_response(
            [_make_offer(f"X{i}", str(i)) for i in range(25, 30)], total=30
        )
        session = _make_session([cats, page1, page2])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse())

        assert len(products) == 30

    def test_browse_max_products(self):
        """max_products stops iteration early."""
        cats = _offer_categories_response(["juomat", "maito-juusto-munat-ja-rasvat"])
        offers1 = _offer_category_response(
            [_make_offer(f"P{i}", str(i)) for i in range(25)], total=25
        )
        session = _make_session([cats, offers1])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse(max_products=3))

        assert len(products) == 3

    def test_browse_empty_categories(self):
        """browse() returns nothing if there are no categories."""
        cats = _offer_categories_response([])
        session = _make_session([cats])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse())

        assert products == []

    def test_browse_uses_post(self):
        """browse() uses POST for all calls."""
        cats = _offer_categories_response(["juomat"])
        offers = _offer_category_response([_make_offer("Kalja", "1")], total=1)
        session = _make_session([cats, offers])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.browse())

        assert not session.get.called
        assert session.post.call_count == 2

    def test_browse_skips_empty_slug(self):
        """Category entries with no slug are skipped."""
        cats = {"offerCategories": [{"slug": "", "count": 0}]}
        session = _make_session([cats])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse())

        assert products == []
        # Only the categories call, no offer-category calls
        assert session.post.call_count == 1

