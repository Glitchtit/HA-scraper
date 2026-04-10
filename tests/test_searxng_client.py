"""Tests for grocy_scraper.searxng_client."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from grocy_scraper.searxng_client import (
    SearXNGError,
    SearXNGProduct,
    _check_kesko_cdn,
    _slug_to_name,
    lookup_ean,
)


# ---------------------------------------------------------------------------
# _slug_to_name
# ---------------------------------------------------------------------------

class TestSlugToName:
    def test_basic_slug(self):
        assert _slug_to_name("capsi-merisuolamylly-230g-barcelona") == "Capsi merisuolamylly 230g barcelona"

    def test_single_word(self):
        assert _slug_to_name("maito") == "Maito"

    def test_empty_slug(self):
        assert _slug_to_name("") == ""


# ---------------------------------------------------------------------------
# _check_kesko_cdn
# ---------------------------------------------------------------------------

class TestCheckKeskoCdn:
    def test_returns_url_on_200_image(self):
        sess = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "image/png"}
        sess.head.return_value = resp

        url = _check_kesko_cdn("6430025642017", sess)
        assert url is not None
        assert "6430025642017" in url
        sess.head.assert_called_once()

    def test_returns_none_on_404(self):
        sess = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 404
        resp.headers = {"content-type": "text/html"}
        sess.head.return_value = resp

        assert _check_kesko_cdn("0000000000000", sess) is None

    def test_returns_none_on_request_error(self):
        sess = MagicMock(spec=requests.Session)
        sess.head.side_effect = requests.ConnectionError("timeout")

        assert _check_kesko_cdn("6430025642017", sess) is None


# ---------------------------------------------------------------------------
# lookup_ean
# ---------------------------------------------------------------------------

EAN = "6430025642017"
SEARXNG = "http://localhost:8181"


def _make_session(json_data, status=200):
    """Build a mock session that returns the given JSON for GET and 200+image for HEAD."""
    sess = MagicMock(spec=requests.Session)

    get_resp = MagicMock()
    get_resp.status_code = status
    get_resp.json.return_value = json_data
    get_resp.text = ""
    sess.get.return_value = get_resp

    head_resp = MagicMock()
    head_resp.status_code = 200
    head_resp.headers = {"content-type": "image/png"}
    sess.head.return_value = head_resp

    return sess


class TestLookupEan:
    def test_strategy1_kruoka_url(self):
        """k-ruoka.fi product URL → name from slug + CDN image."""
        data = {
            "results": [
                {
                    "url": f"https://www.k-ruoka.fi/kauppa/tuote/capsi-merisuolamylly-230g-barcelona-{EAN}",
                    "title": "Capsi Merisuolamylly",
                    "content": "Some description",
                },
            ]
        }
        sess = _make_session(data)
        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

        assert result is not None
        assert result.name == "Capsi merisuolamylly 230g barcelona"
        assert result.ean == EAN
        assert "keskofiles.com" in result.image_url
        assert "k-ruoka.fi" in result.source_url

    def test_strategy2_title_with_ean_in_url(self):
        """No k-ruoka URL but EAN appears in result URL → use title."""
        data = {
            "results": [
                {
                    "url": f"https://foodie.fi/entry/{EAN}",
                    "title": "Capsi Merisuolamylly 230g Barcelona",
                    "content": "Salt grinder",
                },
            ]
        }
        sess = _make_session(data)
        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

        assert result is not None
        assert result.name == "Capsi Merisuolamylly 230g Barcelona"
        assert result.ean == EAN

    def test_strategy2_ean_in_content(self):
        """EAN appears in content from a trusted product domain."""
        data = {
            "results": [
                {
                    "url": f"https://barcodelookup.com/product/{EAN}",
                    "title": "Some Product",
                    "content": f"EAN: {EAN} available at ...",
                },
            ]
        }
        sess = _make_session(data)
        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

        assert result is not None
        assert result.name == "Some Product"

    def test_strategy2_untrusted_domain_skipped(self):
        """Untrusted domains (e.g. trademark databases) are skipped in Strategy 2."""
        data = {
            "results": [
                {
                    "url": f"https://trademarks.justia.com/search?q={EAN}",
                    "title": "GODLY GOAL-GETTER Trademark Application of Reynolds, Sabrina D",
                    "content": f"Serial number {EAN}",
                },
            ]
        }
        sess = _make_session(data)
        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

        # Should fall through to Strategy 3 (CDN image only, not the trademark title)
        assert result is not None
        assert "GODLY" not in result.name
        assert result.name == f"Unknown product ({EAN})"

    def test_strategy3_cdn_only(self):
        """No good name but Kesko CDN has the image."""
        data = {
            "results": [
                {
                    "url": "https://random-site.com/unrelated",
                    "title": "",
                    "content": "Unrelated content",
                },
            ]
        }
        sess = _make_session(data)
        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

        assert result is not None
        assert result.name == f"Unknown product ({EAN})"
        assert "keskofiles.com" in result.image_url

    def test_no_results(self):
        """Empty results list → None."""
        sess = _make_session({"results": []})
        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)
        assert result is None

    def test_no_results_no_cdn(self):
        """Results are irrelevant and CDN returns 404."""
        data = {
            "results": [
                {
                    "url": "https://random.com/page",
                    "title": "Unrelated",
                    "content": "Nothing here",
                },
            ]
        }
        sess = _make_session(data)
        # Override HEAD to return 404 (no CDN image).
        head_resp = MagicMock()
        head_resp.status_code = 404
        head_resp.headers = {"content-type": "text/html"}
        sess.head.return_value = head_resp

        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)
        assert result is None

    def test_http_error_raises(self):
        """Non-200 from SearXNG → SearXNGError."""
        sess = _make_session({}, status=403)
        with pytest.raises(SearXNGError, match="HTTP 403"):
            lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

    def test_connection_error_raises(self):
        """Network error → SearXNGError."""
        sess = MagicMock(spec=requests.Session)
        sess.get.side_effect = requests.ConnectionError("refused")

        with pytest.raises(SearXNGError, match="request failed"):
            lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

    def test_invalid_json_raises(self):
        """Non-JSON response → SearXNGError."""
        sess = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("bad json")
        resp.text = "not json"
        sess.get.return_value = resp

        with pytest.raises(SearXNGError, match="invalid JSON"):
            lookup_ean(EAN, searxng_url=SEARXNG, session=sess)

    def test_trailing_slash_stripped(self):
        """Base URL trailing slash is handled."""
        sess = _make_session({"results": []})
        lookup_ean(EAN, searxng_url="http://localhost:8181/", session=sess)
        call_url = sess.get.call_args[0][0]
        assert "//" not in call_url.split("://")[1]

    def test_kruoka_url_different_ean_skipped(self):
        """k-ruoka URL with different EAN is not matched."""
        data = {
            "results": [
                {
                    "url": "https://www.k-ruoka.fi/kauppa/tuote/some-product-1234567890123",
                    "title": "Other Product",
                    "content": "",
                },
            ]
        }
        sess = _make_session(data)
        # HEAD returns 404 too — no CDN fallback.
        head_resp = MagicMock()
        head_resp.status_code = 404
        head_resp.headers = {"content-type": "text/html"}
        sess.head.return_value = head_resp

        result = lookup_ean(EAN, searxng_url=SEARXNG, session=sess)
        assert result is None
