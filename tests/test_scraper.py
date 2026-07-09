"""Unit tests for the k-ruoka.fi scraper.

Covers both the GraphQL backend (``use_graphql=True``, default) and the
kr-api REST backend (``use_graphql=False``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from scraper.scraper import (
    KRuokaScraper,
    Product,
    StoreAvailability,
    _PRODUCT_CATEGORY_SLUGS,
    _normalize_ean,
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
        from scraper.scraper import _GRAPHQL_MAX_OFFSET, _GRAPHQL_PAGE_SIZE

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

    def test_multi_word_query_sends_first_word_to_api(self):
        """Multi-word queries send only the first word to the upstream API."""
        items = [_gql_product("Lotus Soft Embo 8 rll wc-paperi", "1")]
        session = _make_mock_session([_gql_page(items, total=1)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("lotus paperi"))

        sent_query = session.post.call_args[1]["json"]["variables"]["query"]
        assert sent_query == "lotus"

    def test_single_word_query_sends_full_query_to_api(self):
        """Single-word queries send the full query string to the API."""
        items = [_gql_product("Kevytmaito 1l", "1")]
        session = _make_mock_session([_gql_page(items, total=1)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        list(scraper.search("kevytmaito"))

        sent_query = session.post.call_args[1]["json"]["variables"]["query"]
        assert sent_query == "kevytmaito"

    def test_multi_word_query_respects_max_products(self):
        """max_products is enforced client-side for multi-word queries."""
        items = [
            _gql_product("Lotus Soft Embo 8 rll wc-paperi", "1"),
            _gql_product("Lotus Talous wc-paperi 40rll", "2"),
            _gql_product("Lotus Premium paperi 6rll", "3"),
        ]
        session = _make_mock_session([_gql_page(items, total=3)])
        scraper = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        products = list(scraper.search("lotus paperi", max_products=2))

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

    def test_unwraps_product_and_extracts_mobilescan_price(self):
        # Current v2/product-search shape: hit wraps the product under "product"
        # and carries store-specific pricing in the mobilescan block.
        item = {
            "id": "6410405343260",
            "score": 1.0,
            "product": {
                "ean": "6410405343260",
                "localizedName": {"finnish": "Pirkka suomalainen kevytmaito 1l"},
                "productAttributes": {
                    "image": {"url": "https://public.keskofiles.com/f/k-ruoka/product/6410405343260"},
                },
                "mobilescan": {
                    "pricing": {
                        "normal": {"price": 1.35, "unitPrice": {"value": 1.35, "unit": "l"}},
                        "discount": {"price": 0.99},
                    },
                    "vat": 13.5,
                },
            },
        }
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.name == "Pirkka suomalainen kevytmaito 1l"
        assert p.ean == "6410405343260"
        # Regular price wins over the short-lived discount price.
        assert p.price == 1.35
        assert p.comparison_price == 1.35
        assert p.comparison_unit == "l"
        assert p.image_url.endswith("6410405343260")

    def test_no_pricing_leaves_price_none(self):
        item = _kr_make_search_item("Maito", "1234567890123")
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.price is None


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

    def test_result_singular_key(self):
        # The live v2/product-search response uses the singular "result" key.
        assert KRuokaScraper._extract_search_results({"result": [{"ean": "E"}]}) == [{"ean": "E"}]

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


# ---------------------------------------------------------------------------
# _normalize_ean
# ---------------------------------------------------------------------------

class TestNormalizeEan:
    def test_strips_padding_zeros_to_ean8(self):
        assert _normalize_ean("0000090493508") == "90493508"

    def test_keeps_normal_ean13_unchanged(self):
        assert _normalize_ean("6410405082657") == "6410405082657"

    def test_strips_single_leading_zero_gtin14_to_ean13(self):
        assert _normalize_ean("06410405082657") == "6410405082657"

    def test_keeps_legitimate_gtin14(self):
        assert _normalize_ean("16410405082657") == "16410405082657"

    def test_strips_whitespace(self):
        assert _normalize_ean("  6410405082657  ") == "6410405082657"

    def test_empty_returns_empty(self):
        assert _normalize_ean("") == ""
        assert _normalize_ean(None) == ""  # type: ignore[arg-type]

    def test_non_numeric_unchanged(self):
        assert _normalize_ean("ABC123") == "ABC123"

    def test_all_zeros_preserved(self):
        assert _normalize_ean("0000000000000") == "0000000000000"


# ---------------------------------------------------------------------------
# Leading-zero EAN regression for parse paths
# ---------------------------------------------------------------------------

class TestLeadingZeroEanRegression:
    def test_graphql_product_strips_padding(self):
        item = {
            "__typename": "Product",
            "id": "X",
            "ean": "0000090493508",
            "localizedName": {"finnish": "Tuote"},
        }
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.ean == "90493508"

    def test_graphql_assortment_strips_padding(self):
        item = {
            "__typename": "AssortmentSearchResult",
            "id": "X",
            "eans": ["0000090493508", "6410405082657"],
            "localizedName": {"finnish": "Tuote"},
        }
        p = KRuokaScraper._parse_graphql_result(item)
        assert p is not None
        assert p.ean == "90493508"

    def test_kr_api_search_product_strips_padding(self):
        item = {
            "id": "X",
            "ean": "0000090493508",
            "localizedName": {"finnish": "Tuote"},
        }
        p = KRuokaScraper._parse_search_product(item)
        assert p is not None
        assert p.ean == "90493508"

    def test_offer_product_strips_padding(self):
        offer = {
            "id": "X",
            "product": {
                "product": {
                    "localizedName": {"finnish": "Tuote"},
                    "ean": "0000090493508",
                }
            },
        }
        p = KRuokaScraper._parse_offer_product(offer)
        assert p is not None
        assert p.ean == "90493508"


# ---------------------------------------------------------------------------
# Multi-store search (KRuokaScraper.search with comma-separated store_id)
# ---------------------------------------------------------------------------

class TestMultiStoreSearch:
    def test_search_parses_multi_store(self):
        """store_id='N110,K532,L512' populates store_ids and sets store_id to first."""
        sc = KRuokaScraper(store_id="N110,K532,L512", session=requests.Session())
        assert sc.store_ids == ["N110", "K532", "L512"]
        assert sc.store_id == "N110"

    def test_search_single_store_unchanged(self):
        """store_id='N110' gives store_ids=['N110'] and single-store behaviour."""
        sc = KRuokaScraper(store_id="N110", session=requests.Session())
        assert sc.store_ids == ["N110"]
        assert sc.store_id == "N110"

        # Monkeypatch _search_one_store to return two sentinel products.
        sentinel = [Product(name="p1", ean="1"), Product(name="p2", ean="2")]
        sc._search_one_store = lambda query, max_products=None: iter(sentinel)

        result = list(sc.search("x"))
        assert result == sentinel

    def test_search_multi_store_fallback(self):
        """With store_id='A,B', if A is empty search falls back to B."""
        sc = KRuokaScraper(store_id="A,B", session=requests.Session())

        tried_stores: list[str] = []
        prod_b = Product(name="prodB", ean="B1")

        def fake_search_one_store(query, max_products=None):
            tried_stores.append(sc.store_id)
            if sc.store_id == "B":
                yield prod_b

        sc._search_one_store = fake_search_one_store

        result = list(sc.search("x"))
        assert result == [prod_b]
        assert tried_stores == ["A", "B"]
        # store_id must be restored to the original primary store after search
        assert sc.store_id == "A"

    def test_search_store_id_restored_on_exception(self):
        """store_id is restored even if _search_one_store raises."""
        sc = KRuokaScraper(store_id="A,B", session=requests.Session())

        def boom(query, max_products=None):
            raise RuntimeError("network down")
            yield  # make it a generator

        sc._search_one_store = boom

        with pytest.raises(RuntimeError):
            list(sc.search("x"))

        assert sc.store_id == "A"


# ---------------------------------------------------------------------------
# check_store_availability
# ---------------------------------------------------------------------------

class TestCheckStoreAvailability:
    def test_graphql_found(self):
        session = _make_mock_session(
            [_gql_page([_gql_product("Pirkka kevytmaito 1l", "6410405082657")], 1)]
        )
        s = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        result = s.check_store_availability("6410405082657")
        assert result == [StoreAvailability(store_id="N110", available=True)]

    def test_graphql_not_found_reports_unavailable(self):
        session = _make_mock_session([_gql_page([], 0)])
        s = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        result = s.check_store_availability("6410405082657")
        assert result == [StoreAvailability(store_id="N110", available=False)]

    def test_graphql_error_omits_store(self):
        import requests as _requests
        session = _make_mock_session([_requests.ConnectionError("boom")])
        s = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        assert s.check_store_availability("6410405082657") == []

    def test_multi_store_sweep_mixed(self):
        import requests as _requests
        session = _make_mock_session([
            _gql_page([_gql_product("Maito", "6410405082657")], 1),  # N110: hit
            _gql_page([], 0),                                          # K532: miss
            _requests.ConnectionError("down"),                        # N137: error
        ])
        s = KRuokaScraper(store_id="N110,K532,N137", session=session, request_delay=0)
        result = s.check_store_availability("6410405082657")
        assert result == [
            StoreAvailability(store_id="N110", available=True),
            StoreAvailability(store_id="K532", available=False),
        ]
        # store_id must be restored after the sweep
        assert s.store_id == "N110"

    def test_graphql_assortment_ean_list_matches(self):
        item = _gql_assortment("Pirkka maito", ["6410405082657", "6410405082664"])
        session = _make_mock_session([_gql_page([item], 1)])
        s = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        # The target EAN is the assortment's SECOND ean → still a match.
        result = s.check_store_availability("6410405082664")
        assert result[0].available is True

    def test_normalizes_padded_ean(self):
        session = _make_mock_session(
            [_gql_page([_gql_product("Tuote", "90493508")], 1)]
        )
        s = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        result = s.check_store_availability("0000090493508")
        assert result[0].available is True

    def test_krapi_found_with_price(self):
        item = _kr_make_search_item("Maito", "6410405082657")
        item["mobilescan"] = {"pricing": {"normal": {
            "price": 2.35,
            "unitPrice": {"value": 2.35, "unit": "l"},
        }}}
        session = _make_mock_session([_kr_search_page([item], 1)])
        s = KRuokaScraper(store_id="N110", session=session,
                          request_delay=0, use_graphql=False)
        result = s.check_store_availability("6410405082657")
        assert result == [StoreAvailability(store_id="N110", available=True,
                                            price=2.35, price_currency="EUR")]

    def test_krapi_not_found(self):
        session = _make_mock_session([_kr_search_page([], 0)])
        s = KRuokaScraper(store_id="N110", session=session,
                          request_delay=0, use_graphql=False)
        result = s.check_store_availability("6410405082657")
        assert result == [StoreAvailability(store_id="N110", available=False)]

    def test_empty_ean_returns_empty(self):
        session = _make_mock_session([])
        s = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        assert s.check_store_availability("") == []


# ---------------------------------------------------------------------------
# fetch_store_name
# ---------------------------------------------------------------------------

class TestFetchStoreName:
    def test_graphql_backend_returns_none_without_request(self):
        session = _make_mock_session([])
        s = KRuokaScraper(store_id="N110", session=session, request_delay=0)
        assert s.fetch_store_name("N110") is None
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_krapi_store_endpoint_normalizes_unicode_hyphen(self):
        session = _make_mock_session([])
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "N141",
            "name": "K‑Citymarket Pirkkala",
            "chainName": "K-Citymarket",
            "shortName": "K‑Citymarket Pirkkala",
        }
        mock_resp.raise_for_status.return_value = None
        session.get.return_value = mock_resp

        s = KRuokaScraper(store_id="N141", session=session,
                          request_delay=0, use_graphql=False)
        assert s.fetch_store_name("N141") == "K-Citymarket Pirkkala"
        session.get.assert_called_once()
        called_url = session.get.call_args[0][0]
        assert called_url.endswith("/kr-api/store/N141")

    def test_krapi_request_error_returns_none(self):
        session = _make_mock_session([])
        session.get.side_effect = requests.ConnectionError("cf")
        s = KRuokaScraper(store_id="N110", session=session,
                          request_delay=0, use_graphql=False)
        assert s.fetch_store_name("N110") is None

    def test_krapi_non_dict_response_returns_none(self):
        session = _make_mock_session([])
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": "N110", "name": "Should not be used"}]
        mock_resp.raise_for_status.return_value = None
        session.get.return_value = mock_resp

        s = KRuokaScraper(store_id="N110", session=session,
                          request_delay=0, use_graphql=False)
        assert s.fetch_store_name("N110") is None

    def test_krapi_missing_name_falls_back_to_short_name(self):
        session = _make_mock_session([])
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": "N110",
            "shortName": "K‑Citymarket Kupittaa",
        }
        mock_resp.raise_for_status.return_value = None
        session.get.return_value = mock_resp

        s = KRuokaScraper(store_id="N110", session=session,
                          request_delay=0, use_graphql=False)
        assert s.fetch_store_name("N110") == "K-Citymarket Kupittaa"

    def test_krapi_empty_body_returns_none(self):
        session = _make_mock_session([])
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.return_value = None
        session.get.return_value = mock_resp

        s = KRuokaScraper(store_id="N110", session=session,
                          request_delay=0, use_graphql=False)
        assert s.fetch_store_name("N110") is None


# ---------------------------------------------------------------------------
# kr-api rate limiting (429 backoff + global throttle)
# ---------------------------------------------------------------------------

def _make_429_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {}
    resp.raise_for_status.side_effect = requests.HTTPError(
        "429 Client Error: Too Many Requests", response=resp
    )
    return resp


def _make_ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


class TestKrApiRateLimiting:
    def _scraper(self, session: MagicMock) -> KRuokaScraper:
        return KRuokaScraper(
            store_id="N110", session=session, request_delay=0, use_graphql=False
        )

    def test_429_is_retried_with_backoff(self, monkeypatch):
        """A 429 response is retried after a backoff instead of failing."""
        sleeps: list[float] = []
        monkeypatch.setattr(
            "scraper.scraper.time.sleep", lambda s: sleeps.append(s)
        )
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.post.side_effect = [
            _make_429_response(),
            _make_ok_response({"result": []}),
        ]

        s = self._scraper(session)
        data = s._post_json("https://www.k-ruoka.fi/kr-api/v2/product-search/x", None)

        assert data == {"result": []}
        assert session.post.call_count == 2
        assert any(sl > 0 for sl in sleeps), "expected a backoff sleep before retry"

    def test_429_gives_up_after_max_retries(self, monkeypatch):
        """Persistent 429s eventually give up and return None."""
        from scraper.scraper import _RATE_LIMIT_MAX_RETRIES

        monkeypatch.setattr("scraper.scraper.time.sleep", lambda s: None)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.post.return_value = _make_429_response()

        s = self._scraper(session)
        data = s._post_json("https://www.k-ruoka.fi/kr-api/v2/product-search/x", None)

        assert data is None
        assert session.post.call_count == _RATE_LIMIT_MAX_RETRIES + 1

    def test_429_honors_retry_after_header(self, monkeypatch):
        """A Retry-After header sets the backoff duration."""
        sleeps: list[float] = []
        monkeypatch.setattr(
            "scraper.scraper.time.sleep", lambda s: sleeps.append(s)
        )
        resp_429 = _make_429_response()
        resp_429.headers = {"Retry-After": "7"}
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.post.side_effect = [resp_429, _make_ok_response({"result": []})]

        s = self._scraper(session)
        data = s._post_json("https://www.k-ruoka.fi/kr-api/v2/product-search/x", None)

        assert data == {"result": []}
        # Real clock ticks between penalize() and wait(), so allow slack.
        assert any(sl >= 6.5 for sl in sleeps)

    def test_get_json_also_rate_limited(self, monkeypatch):
        """_get_json shares the same 429 retry logic."""
        sleeps: list[float] = []
        monkeypatch.setattr(
            "scraper.scraper.time.sleep", lambda s: sleeps.append(s)
        )
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = [
            _make_429_response(),
            _make_ok_response({"name": "K-Citymarket X"}),
        ]

        s = self._scraper(session)
        data = s._get_json("https://www.k-ruoka.fi/kr-api/store/N110")

        assert data == {"name": "K-Citymarket X"}
        assert session.get.call_count == 2

    def test_global_throttle_spaces_requests(self, monkeypatch):
        """Consecutive kr-api requests are spaced by the global min interval,
        even across separate scraper instances."""
        from scraper.scraper import _KrApiThrottle

        clock = {"now": 0.0}
        sleeps: list[float] = []

        def fake_sleep(s):
            sleeps.append(s)
            clock["now"] += s

        monkeypatch.setattr("scraper.scraper.time.monotonic", lambda: clock["now"])
        monkeypatch.setattr("scraper.scraper.time.sleep", fake_sleep)

        throttle = _KrApiThrottle(min_interval=1.0)
        throttle.wait()   # first request: no wait
        throttle.wait()   # second request: must wait ~1s

        assert sleeps and abs(sum(sleeps) - 1.0) < 0.01

    def test_throttle_penalty_delays_next_request(self, monkeypatch):
        """A 429 penalty pushes back the next allowed request time."""
        from scraper.scraper import _KrApiThrottle

        clock = {"now": 0.0}
        sleeps: list[float] = []

        def fake_sleep(s):
            sleeps.append(s)
            clock["now"] += s

        monkeypatch.setattr("scraper.scraper.time.monotonic", lambda: clock["now"])
        monkeypatch.setattr("scraper.scraper.time.sleep", fake_sleep)

        throttle = _KrApiThrottle(min_interval=1.0)
        throttle.wait()
        throttle.penalize(30.0)
        throttle.wait()

        assert any(sl >= 29 for sl in sleeps)


# ---------------------------------------------------------------------------
# FlareSolverr endpoint normalization & backoff cap
# ---------------------------------------------------------------------------

class TestFlareSolverrEndpoint:
    def test_bare_host_gets_v1_appended(self):
        from scraper.scraper import _flaresolverr_endpoint
        assert _flaresolverr_endpoint("http://192.168.50.111:8191") == \
            "http://192.168.50.111:8191/v1"

    def test_trailing_slash_stripped(self):
        from scraper.scraper import _flaresolverr_endpoint
        assert _flaresolverr_endpoint("http://host:8191/") == "http://host:8191/v1"

    def test_existing_v1_kept(self):
        from scraper.scraper import _flaresolverr_endpoint
        assert _flaresolverr_endpoint("http://host:8191/v1") == "http://host:8191/v1"

    def test_resolve_posts_to_v1(self, monkeypatch):
        """_resolve_cf_flaresolverr must hit the /v1 API endpoint even when
        FLARESOLVERR_URL is configured as the bare root (which 405s on POST)."""
        from scraper.scraper import _resolve_cf_flaresolverr

        monkeypatch.setenv("FLARESOLVERR_URL", "http://192.168.50.111:8191")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "status": "ok",
            "solution": {
                "cookies": [{"name": "cf_clearance", "value": "x"}],
                "userAgent": "UA",
            },
        }
        post = MagicMock(return_value=resp)
        monkeypatch.setattr("scraper.scraper.requests.post", post)

        cookies, ua = _resolve_cf_flaresolverr("https://www.k-ruoka.fi/kauppa")

        assert cookies == {"cf_clearance": "x"}
        called_url = post.call_args[0][0]
        assert called_url.endswith("/v1")


class TestRetryAfterCap:
    def test_huge_retry_after_is_capped(self):
        """Cloudflare can send absurd Retry-After values (hours); cap them."""
        from scraper.scraper import _retry_after_seconds, _RATE_LIMIT_BACKOFF_MAX

        resp = MagicMock()
        resp.headers = {"Retry-After": "6188"}
        assert _retry_after_seconds(resp, 0) == _RATE_LIMIT_BACKOFF_MAX

    def test_small_retry_after_used_as_is(self):
        from scraper.scraper import _retry_after_seconds

        resp = MagicMock()
        resp.headers = {"Retry-After": "7"}
        assert _retry_after_seconds(resp, 0) == 7.0
