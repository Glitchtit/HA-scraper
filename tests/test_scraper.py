"""Unit tests for the k-ruoka.fi scraper.

Covers both the GraphQL backend (``use_graphql=True``, default) and the
kr-api REST backend (``use_graphql=False``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from grocy_scraper.scraper import (
    KRuokaScraper,
    Product,
    _PRODUCT_CATEGORY_SLUGS,
)


# ---------------------------------------------------------------------------
# Mock session helpers
# ---------------------------------------------------------------------------

def _make_mock_session(responses: list[dict | Exception]) -> MagicMock:
    """Return a mock session whose .post() returns *responses* in order."""
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    session.cookies = MagicMock()
    session.get = MagicMock()

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


# ------------------------------------------------------------------
# GraphQL response builders
# ------------------------------------------------------------------

def _gql_page(items: list[dict], total: int) -> dict:
    """Wrap items in a GraphQL productAndAssortmentSearchV2 response."""
    return {
        "data": {
            "productAndAssortmentSearchV2": {
                "results": items,
                "totalHits": total,
                "__typename": "ProductAndAssortmentSearchResult",
            }
        }
    }


def _gql_product(name: str, ean: str, image_url: str = "") -> dict:
    """Build a GraphQL Product result item."""
    return {
        "__typename": "Product",
        "id": ean,
        "ean": ean,
        "localizedName": {"finnish": name},
        "imageUrl": image_url,
        "productType": "NORMAL",
    }


def _gql_assortment(name: str, eans: list[str]) -> dict:
    """Build a GraphQL AssortmentSearchResult item."""
    return {
        "__typename": "AssortmentSearchResult",
        "id": f"ASSORT_{eans[0]}",
        "eans": eans,
        "localizedName": {"finnish": name},
        "imageUrl": "",
        "productType": "NORMAL",
    }


# ------------------------------------------------------------------
# kr-api response builders
# ------------------------------------------------------------------

def _kr_search_page(items: list[dict], total: int) -> dict:
    return {"results": items, "totalHits": total}


def _kr_offer_categories_response(slugs: list[str]) -> dict:
    return {
        "offerCategories": [
            {"slug": s, "count": 1, "name": {"finnish": s}} for s in slugs
        ]
    }


def _kr_offer_category_response(offers: list[dict], total: int) -> dict:
    return {"offers": offers, "totalHits": total}


def _kr_make_offer(name: str, ean: str) -> dict:
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


def _kr_make_search_item(name: str, ean: str) -> dict:
    return {"id": ean, "ean": ean, "localizedName": {"finnish": name}}


# ---------------------------------------------------------------------------
# _parse_graphql_result
# ---------------------------------------------------------------------------

class TestParseGraphqlResult:
    def test_product_standard_shape(self):
        item = _gql_product("Pirkka maito 1l", "6410405082657")
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.name == "Pirkka maito 1l"
        assert p.ean == "6410405082657"
        assert p.product_id == "6410405082657"

    def test_assortment_uses_first_ean(self):
        item = _gql_assortment("Pirkka maito", ["111", "222", "333"])
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.ean == "111"
        assert p.name == "Pirkka maito"

    def test_image_url_extracted(self):
        item = _gql_product("Maito", "123", image_url="https://example.com/img.jpg")
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.image_url == "https://example.com/img.jpg"

    def test_missing_name_and_ean_returns_none(self):
        assert KRuokaScraper._parse_graphql_result({}) is None

    def test_empty_assortment_eans_returns_none(self):
        item = {
            "__typename": "AssortmentSearchResult",
            "id": "X",
            "eans": [],
            "localizedName": {"finnish": ""},
        }
        assert KRuokaScraper._parse_graphql_result(item) is None

    def test_strips_whitespace(self):
        item = {
            "__typename": "Product",
            "id": "  123  ",
            "ean": "  123  ",
            "localizedName": {"finnish": "  Maito  "},
        }
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.name == "Maito"
        assert p.ean == "123"

    def test_localized_name_fi_fallback(self):
        item = {
            "__typename": "Product",
            "id": "42",
            "ean": "42",
            "localizedName": {"fi": "Voi"},
        }
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.name == "Voi"

    def test_unknown_typename_treated_as_product(self):
        """Items without __typename default to the Product parsing path."""
        item = {"id": "99", "ean": "99", "localizedName": {"finnish": "Kerma"}}
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.ean == "99"


# ---------------------------------------------------------------------------
# GraphQL search – _paginate_graphql
# ---------------------------------------------------------------------------

class TestGraphqlSearch:
    def test_single_page(self):
        items = [_gql_product("Maito", "111"), _gql_product("Maitojuoma", "222")]
        session = _make_mock_session([_gql_page(items, total=2)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("maito"))

        assert len(products) == 2
        assert products[0].name == "Maito"
        assert products[1].ean == "222"
        assert session.post.call_count == 1

    def test_posts_to_graphql_url(self):
        session = _make_mock_session([_gql_page([], 0)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("maito"))

        call_url = session.post.call_args[0][0]
        assert "mobile.k-ruoka.fi" in call_url
        assert "graphql" in call_url

    def test_query_in_variables(self):
        session = _make_mock_session([_gql_page([], 0)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("juusto"))

        payload = session.post.call_args[1]["json"]
        assert payload["variables"]["query"] == "juusto"
        assert payload["variables"]["storeId"] == "N110"

    def test_uses_post_not_get(self):
        session = _make_mock_session([_gql_page([], 0)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("maito"))

        assert session.post.called
        assert not session.get.called

    def test_pagination(self):
        p1 = _gql_page(
            [_gql_product(f"P{i}", str(i)) for i in range(100)], total=150
        )
        p2 = _gql_page(
            [_gql_product(f"P{i}", str(i)) for i in range(100, 150)], total=150
        )
        session = _make_mock_session([p1, p2])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("P"))

        assert len(products) == 150
        assert session.post.call_count == 2

    def test_stops_on_partial_page(self):
        """A page with fewer than 100 items signals the end of results."""
        p1 = _gql_page([_gql_product("Ax", "1")], total=500)
        session = _make_mock_session([p1])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("x"))

        assert len(products) == 1
        assert session.post.call_count == 1

    def test_stops_on_empty_page(self):
        p1 = _gql_page([_gql_product("Ax", "1")], total=500)
        p2 = _gql_page([], total=500)
        session = _make_mock_session([p1, p2])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("x"))

        assert len(products) == 1

    def test_stops_on_graphql_error(self):
        """GraphQL error responses (data=None with errors) stop iteration."""
        error_resp = {"errors": [{"message": "Internal server error"}], "data": None}
        session = _make_mock_session([error_resp])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("x"))

        assert products == []

    def test_max_products(self):
        p1 = _gql_page(
            [_gql_product(f"P{i}", str(i)) for i in range(100)], total=500
        )
        session = _make_mock_session([p1])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("P", max_products=5))

        assert len(products) == 5

    def test_handles_http_error(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.cookies = MagicMock()
        session.get = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=500)
        )
        session.post.return_value = mock_resp

        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("x"))
        assert products == []

    def test_assortment_result_parsed(self):
        items = [_gql_assortment("Pirkka maito assortment", ["100", "101"])]
        session = _make_mock_session([_gql_page(items, total=1)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("maito"))

        assert len(products) == 1
        assert products[0].ean == "100"
        assert products[0].name == "Pirkka maito assortment"

    def test_does_not_exceed_max_offset(self):
        """Should not request past _GRAPHQL_MAX_OFFSET."""
        from grocy_scraper.scraper import _GRAPHQL_MAX_OFFSET, _GRAPHQL_PAGE_SIZE

        # Return full pages until we'd go past the max offset
        pages = []
        for _ in range((_GRAPHQL_MAX_OFFSET // _GRAPHQL_PAGE_SIZE) + 2):
            pages.append(
                _gql_page(
                    [_gql_product(f"P{i}", str(i)) for i in range(_GRAPHQL_PAGE_SIZE)],
                    total=99999,
                )
            )
        session = _make_mock_session(pages)
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("P"))

        # Extract all offset values used
        offsets = [
            call[1]["json"]["variables"]["offset"]
            for call in session.post.call_args_list
        ]
        assert all(o <= _GRAPHQL_MAX_OFFSET for o in offsets), (
            f"Offset exceeded max: {offsets}"
        )

    def test_filters_non_matching_products(self):
        """Products whose name does not contain the query are excluded."""
        items = [
            _gql_product("Kevytmaito 1l", "1"),
            _gql_product("Maitosuklaa", "2"),
            _gql_product("Pirkka kevytmaito", "3"),
            _gql_product("Kerma 2dl", "4"),
        ]
        session = _make_mock_session([_gql_page(items, total=4)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("kevytmaito"))

        assert [p.name for p in products] == ["Kevytmaito 1l", "Pirkka kevytmaito"]

    def test_filter_is_case_insensitive(self):
        items = [_gql_product("KEVYTMAITO 1L", "1"), _gql_product("Kerma", "2")]
        session = _make_mock_session([_gql_page(items, total=2)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("kevytmaito"))

        assert len(products) == 1
        assert products[0].name == "KEVYTMAITO 1L"

    def test_multi_word_query_matches_non_contiguous_words(self):
        """Multi-word queries match when all words appear in the name."""
        items = [
            _gql_product("Lotus Soft Embo 8 rll wc-paperi", "1"),
            _gql_product("Serla WC-paperi 24rll", "2"),
            _gql_product("Lotus käsipyyhe", "3"),
            _gql_product("Kahvi 500g", "4"),
        ]
        session = _make_mock_session([_gql_page(items, total=4)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("lotus paperi"))

        assert [p.name for p in products] == ["Lotus Soft Embo 8 rll wc-paperi"]

    def test_single_word_query_still_works(self):
        """Single-word queries continue to work as substring matches."""
        items = [
            _gql_product("Lotus Soft Embo 8 rll wc-paperi", "1"),
            _gql_product("Serla WC-paperi 24rll", "2"),
        ]
        session = _make_mock_session([_gql_page(items, total=2)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("paperi"))

        assert len(products) == 2


# ---------------------------------------------------------------------------
# GraphQL browse – _browse_graphql
# ---------------------------------------------------------------------------

class TestGraphqlBrowse:
    def test_browse_iterates_categories(self):
        """browse() iterates through _PRODUCT_CATEGORY_SLUGS."""
        # Provide one product per category (uses first slug)
        first_slug = _PRODUCT_CATEGORY_SLUGS[0]
        page = _gql_page([_gql_product("Kurkku", "200")], total=1)

        # One response per category (all empty except the first)
        responses = [page] + [
            _gql_page([], total=0) for _ in _PRODUCT_CATEGORY_SLUGS[1:]
        ]
        session = _make_mock_session(responses)
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse())

        assert len(products) == 1
        assert products[0].ean == "200"

    def test_browse_passes_category_path(self):
        """Each category request must include the categoryPath variable."""
        # Give every category exactly one page
        first_page = _gql_page([_gql_product("A", "1")], total=1)
        responses = [first_page] + [
            _gql_page([], 0) for _ in _PRODUCT_CATEGORY_SLUGS[1:]
        ]
        session = _make_mock_session(responses)
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.browse())

        first_call_vars = session.post.call_args_list[0][1]["json"]["variables"]
        assert "categoryPath" in first_call_vars
        assert first_call_vars["categoryPath"] == _PRODUCT_CATEGORY_SLUGS[0]

    def test_browse_deduplicates_by_ean(self):
        """Products appearing in multiple categories are returned only once."""
        shared_ean = "SHARED_EAN"
        page1 = _gql_page([_gql_product("SharedProduct", shared_ean)], total=1)
        page2 = _gql_page([_gql_product("SharedProduct", shared_ean)], total=1)
        rest = [_gql_page([], 0) for _ in _PRODUCT_CATEGORY_SLUGS[2:]]

        session = _make_mock_session([page1, page2] + rest)
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse())

        assert len(products) == 1
        assert products[0].ean == shared_ean

    def test_browse_max_products(self):
        page = _gql_page(
            [_gql_product(f"P{i}", str(i)) for i in range(100)], total=200
        )
        responses = [page] * len(_PRODUCT_CATEGORY_SLUGS) * 2
        session = _make_mock_session(responses)
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.browse(max_products=3))

        assert len(products) == 3

    def test_browse_uses_graphql_url(self):
        page = _gql_page([_gql_product("A", "1")], total=1)
        responses = [page] + [
            _gql_page([], 0) for _ in _PRODUCT_CATEGORY_SLUGS[1:]
        ]
        session = _make_mock_session(responses)
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.browse())

        call_url = session.post.call_args_list[0][0][0]
        assert "mobile.k-ruoka.fi" in call_url

    def test_browse_uses_post_not_get(self):
        page = _gql_page([_gql_product("A", "1")], total=1)
        responses = [page] + [
            _gql_page([], 0) for _ in _PRODUCT_CATEGORY_SLUGS[1:]
        ]
        session = _make_mock_session(responses)
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.browse())

        assert not session.get.called

    def test_browse_category_slug_list_non_empty(self):
        """_PRODUCT_CATEGORY_SLUGS must be a non-empty list of strings."""
        assert isinstance(_PRODUCT_CATEGORY_SLUGS, list)
        assert len(_PRODUCT_CATEGORY_SLUGS) > 0
        for slug in _PRODUCT_CATEGORY_SLUGS:
            assert isinstance(slug, str) and slug


# ---------------------------------------------------------------------------
# use_graphql=False – kr-api backend (regression)
# ---------------------------------------------------------------------------

class TestKrapiSearch:
    def test_search_single_page(self):
        page = _kr_search_page(
            [_kr_make_search_item("Kevytmaito", "111"), _kr_make_search_item("Rasvaton maito", "222")],
            total=2,
        )
        session = _make_mock_session([page])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.search("maito"))

        assert len(products) == 2
        assert products[0].name == "Kevytmaito"
        assert products[1].ean == "222"

    def test_search_posts_to_krapi(self):
        session = _make_mock_session([_kr_search_page([], 0)])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        list(scraper.search("maito"))

        call_url = session.post.call_args[0][0]
        assert "k-ruoka.fi/kr-api" in call_url
        assert "maito" in call_url

    def test_search_pagination(self):
        p1 = _kr_search_page(
            [_kr_make_search_item(f"P{i}", str(i)) for i in range(100)], total=110
        )
        p2 = _kr_search_page(
            [_kr_make_search_item(f"P{i}", str(i)) for i in range(100, 110)], total=110
        )
        session = _make_mock_session([p1, p2])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.search("P"))

        assert len(products) == 110
        assert session.post.call_count == 2

    def test_search_max_products(self):
        page = _kr_search_page(
            [_kr_make_search_item(f"P{i}", str(i)) for i in range(100)], total=500
        )
        session = _make_mock_session([page])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.search("P", max_products=5))

        assert len(products) == 5

    def test_search_stops_on_empty_page(self):
        p1 = _kr_search_page([_kr_make_search_item("Ax", "1")], total=10)
        p2 = _kr_search_page([], total=10)
        session = _make_mock_session([p1, p2])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.search("x"))

        assert len(products) == 1

    def test_search_handles_http_error(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.cookies = MagicMock()
        session.get = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=500)
        )
        session.post.return_value = mock_resp

        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.search("x"))
        assert products == []


class TestKrapiBrowse:
    def test_browse_fetches_categories_first(self):
        cats = _kr_offer_categories_response(["juomat"])
        offers = _kr_offer_category_response([_kr_make_offer("Kalja", "333")], total=1)
        session = _make_mock_session([cats, offers])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.browse())

        assert len(products) == 1
        assert products[0].name == "Kalja"
        assert products[0].ean == "333"
        assert session.post.call_count == 2

    def test_browse_iterates_multiple_categories(self):
        cats = _kr_offer_categories_response(
            ["juomat", "maito-juusto-munat-ja-rasvat"]
        )
        offers1 = _kr_offer_category_response([_kr_make_offer("Kalja", "1")], total=1)
        offers2 = _kr_offer_category_response([_kr_make_offer("Maito", "2")], total=1)
        session = _make_mock_session([cats, offers1, offers2])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.browse())

        assert len(products) == 2
        assert {p.ean for p in products} == {"1", "2"}

    def test_browse_max_products(self):
        cats = _kr_offer_categories_response(
            ["juomat", "maito-juusto-munat-ja-rasvat"]
        )
        offers1 = _kr_offer_category_response(
            [_kr_make_offer(f"P{i}", str(i)) for i in range(25)], total=25
        )
        session = _make_mock_session([cats, offers1])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.browse(max_products=3))

        assert len(products) == 3

    def test_browse_paginates_category(self):
        cats = _kr_offer_categories_response(["juomat"])
        page1 = _kr_offer_category_response(
            [_kr_make_offer(f"X{i}", str(i)) for i in range(25)], total=30
        )
        page2 = _kr_offer_category_response(
            [_kr_make_offer(f"X{i}", str(i)) for i in range(25, 30)], total=30
        )
        session = _make_mock_session([cats, page1, page2])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.browse())

        assert len(products) == 30

    def test_browse_skips_empty_slug(self):
        cats = {"offerCategories": [{"slug": "", "count": 0}]}
        session = _make_mock_session([cats])
        scraper = KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )
        products = list(scraper.browse())

        assert products == []
        assert session.post.call_count == 1


# ---------------------------------------------------------------------------
# _parse_search_product (kr-api – regression)
# ---------------------------------------------------------------------------

class TestParseSearchProduct:
    def test_standard_keys(self):
        item = _kr_make_search_item("Maito", "1234567890123")
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.name == "Maito"
        assert p.ean == "1234567890123"

    def test_localized_name_preferred(self):
        item = {"name": "En", "ean": "111", "localizedName": {"finnish": "Fi"}}
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.name == "Fi"

    def test_fallback_to_flat_name(self):
        p = KRuokaScraper._parse_search_product({"name": "Tuote", "ean": "222"})
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


# ---------------------------------------------------------------------------
# _extract_search_results (kr-api – regression)
# ---------------------------------------------------------------------------

class TestExtractSearchResults:
    def test_results_key(self):
        assert KRuokaScraper._extract_search_results({"results": [{"ean": "1"}]}) == [{"ean": "1"}]

    def test_products_key(self):
        assert KRuokaScraper._extract_search_results({"products": [{"ean": "A"}]}) == [{"ean": "A"}]

    def test_items_key(self):
        assert KRuokaScraper._extract_search_results({"items": [{"ean": "B"}]}) == [{"ean": "B"}]

    def test_data_key(self):
        assert KRuokaScraper._extract_search_results({"data": [{"ean": "C"}]}) == [{"ean": "C"}]

    def test_bare_list(self):
        assert KRuokaScraper._extract_search_results([{"ean": "D"}]) == [{"ean": "D"}]  # type: ignore[arg-type]

    def test_empty_on_unknown_shape(self):
        assert KRuokaScraper._extract_search_results({"unknown": "val"}) == []


# ---------------------------------------------------------------------------
# _extract_total (kr-api – regression)
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
# _parse_offer_product (kr-api – regression)
# ---------------------------------------------------------------------------

class TestParseOfferProduct:
    def test_standard_offer_shape(self):
        offer = _kr_make_offer("Porkkanasose", "6418248002382")
        p = KRuokaScraper._parse_offer_product(offer)
        assert p is not None
        assert p.name == "Porkkanasose"
        assert p.ean == "6418248002382"

    def test_ean_from_product_attributes(self):
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


