"""Tests for grocy_scraper.skaupat_client."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from grocy_scraper.skaupat_client import (
    SKaupatError,
    SKaupatProduct,
    lookup_ean,
)


def _build_html(apollo_state: dict) -> str:
    """Wrap an Apollo state dict into a minimal S-kaupat product page."""
    next_data = {
        "props": {
            "pageProps": {
                "apolloState": apollo_state,
            }
        },
        "page": "/tuote/[...eanAndSlug]",
        "buildId": "test",
    }
    return (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script></body></html>"
    )


PRODUCT_APOLLO = {
    "Product:6414893095588": {
        "__typename": "Product",
        "ean": "6414893095588",
        "name": "Kotimaista luomukananmunat M6",
        "description": "Luomukananmunia kennossa 6 kpl.",
        "brandName": "Kotimaista",
        "slug": "kotimaista-luomukananmunat-m6",
    },
    "ROOT_QUERY": {"__typename": "Query"},
}


class TestLookupEan:
    """Tests for lookup_ean()."""

    def _mock_session(self, status_code: int, text: str = "") -> MagicMock:
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        session.get.return_value = resp
        return session

    def test_returns_product_on_200(self):
        html = _build_html(PRODUCT_APOLLO)
        session = self._mock_session(200, html)

        result = lookup_ean("6414893095588", session=session)

        assert result is not None
        assert isinstance(result, SKaupatProduct)
        assert result.name == "Kotimaista luomukananmunat M6"
        assert result.ean == "6414893095588"
        assert result.description == "Luomukananmunia kennossa 6 kpl."
        assert result.brand == "Kotimaista"
        assert "6414893095588" in result.image_url

    def test_returns_none_on_404(self):
        session = self._mock_session(404)
        result = lookup_ean("0000000000000", session=session)
        assert result is None

    def test_raises_on_unexpected_status(self):
        session = self._mock_session(500)
        with pytest.raises(SKaupatError, match="Unexpected HTTP 500"):
            lookup_ean("1234567890123", session=session)

    def test_raises_on_missing_next_data(self):
        session = self._mock_session(200, "<html><body></body></html>")
        with pytest.raises(SKaupatError, match="__NEXT_DATA__"):
            lookup_ean("1234567890123", session=session)

    def test_raises_on_invalid_json(self):
        html = (
            '<html><body><script id="__NEXT_DATA__" type="application/json">'
            "{bad json}"
            "</script></body></html>"
        )
        session = self._mock_session(200, html)
        with pytest.raises(SKaupatError, match="Failed to parse"):
            lookup_ean("1234567890123", session=session)

    def test_returns_none_when_no_product_in_apollo(self):
        html = _build_html({"ROOT_QUERY": {"__typename": "Query"}})
        session = self._mock_session(200, html)
        result = lookup_ean("9999999999999", session=session)
        assert result is None

    def test_returns_none_when_ean_mismatch(self):
        html = _build_html(PRODUCT_APOLLO)
        session = self._mock_session(200, html)
        result = lookup_ean("0000000000000", session=session)
        assert result is None

    def test_raises_on_request_exception(self):
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("timeout")
        with pytest.raises(SKaupatError, match="HTTP request failed"):
            lookup_ean("1234567890123", session=session)

    def test_skips_product_with_empty_name(self):
        apollo = {
            "Product:123": {
                "__typename": "Product",
                "ean": "1234567890123",
                "name": "",
                "description": "some desc",
            },
        }
        html = _build_html(apollo)
        session = self._mock_session(200, html)
        result = lookup_ean("1234567890123", session=session)
        assert result is None

    def test_multiple_products_returns_matching_ean(self):
        apollo = {
            "Product:111": {
                "__typename": "Product",
                "ean": "1111111111111",
                "name": "Other Product",
            },
            "Product:222": {
                "__typename": "Product",
                "ean": "2222222222222",
                "name": "Target Product",
                "description": "desc",
                "brandName": "Brand",
            },
        }
        html = _build_html(apollo)
        session = self._mock_session(200, html)
        result = lookup_ean("2222222222222", session=session)
        assert result is not None
        assert result.name == "Target Product"
