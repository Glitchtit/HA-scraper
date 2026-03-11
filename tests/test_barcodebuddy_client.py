"""Tests for BarcodeBuddyClient."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from grocy_scraper.barcodebuddy_client import (
    BarcodeBuddyClient,
    BarcodeBuddyError,
    UnknownBarcode,
)


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

# Minimal HTML matching Barcode Buddy's unknown barcodes table structure.
_HTML_WITH_UNKNOWNS = """
<html><body>
<form id="f2" method="post">
<table>
<tr><th>Barcode</th><th>Look up</th><th>Quantity</th><th>Product</th>
<th>Action</th><th>Create</th><th>Remove</th></tr>
<tr>
  <td>6410405082657</td>
  <td><a href="https://google.com/search?q=6410405082657">Search</a></td>
  <td>1</td>
  <td><select id="select_42" name="select_42"><option>--</option></select></td>
  <td><button name="button_add" value="42">Add</button></td>
  <td><button name="button_createproduct">Create</button></td>
  <td><button name="button_delete" value="42">Remove</button></td>
</tr>
<tr>
  <td>5901234123457</td>
  <td><a href="https://google.com/search?q=5901234123457">Search</a></td>
  <td>3</td>
  <td><select id="select_99" name="select_99"><option>--</option></select></td>
  <td><button name="button_add" value="99">Add</button></td>
  <td><button name="button_createproduct">Create</button></td>
  <td><button name="button_delete" value="99">Remove</button></td>
</tr>
</table>
</form>
</body></html>
"""

_HTML_NO_UNKNOWNS = """
<html><body>
<form id="f2" method="post">
<p>No unknown barcodes yet.</p>
</form>
</body></html>
"""

_HTML_NO_SECTION = """
<html><body><p>Something else entirely</p></body></html>
"""


# ---------------------------------------------------------------------------
# _parse_unknown_barcodes (static method, no HTTP)
# ---------------------------------------------------------------------------

class TestParseUnknownBarcodes:
    def test_parses_two_barcodes(self):
        result = BarcodeBuddyClient._parse_unknown_barcodes(_HTML_WITH_UNKNOWNS)
        assert len(result) == 2
        assert result[0] == UnknownBarcode(id="42", barcode="6410405082657", amount="1")
        assert result[1] == UnknownBarcode(id="99", barcode="5901234123457", amount="3")

    def test_no_unknown_barcodes(self):
        result = BarcodeBuddyClient._parse_unknown_barcodes(_HTML_NO_UNKNOWNS)
        assert result == []

    def test_no_f2_section(self):
        result = BarcodeBuddyClient._parse_unknown_barcodes(_HTML_NO_SECTION)
        assert result == []


# ---------------------------------------------------------------------------
# get_unknown_barcodes (HTTP)
# ---------------------------------------------------------------------------

class TestGetUnknownBarcodes:
    def test_success(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        resp = MagicMock()
        resp.text = _HTML_WITH_UNKNOWNS
        resp.raise_for_status.return_value = None
        session.get.return_value = resp

        client = BarcodeBuddyClient("https://bb.example.com", "key", session=session)
        unknowns = client.get_unknown_barcodes()

        assert len(unknowns) == 2
        session.get.assert_called_once()
        url_arg = session.get.call_args[0][0]
        assert "bb.example.com/index.php" in url_arg

    def test_http_error_raises(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = requests.RequestException("timeout")

        client = BarcodeBuddyClient("https://bb.example.com", "key", session=session)
        with pytest.raises(BarcodeBuddyError, match="Failed to fetch"):
            client.get_unknown_barcodes()


# ---------------------------------------------------------------------------
# delete_barcode
# ---------------------------------------------------------------------------

class TestDeleteBarcode:
    def test_success(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        session.post.return_value = resp

        client = BarcodeBuddyClient("https://bb.example.com", "key", session=session)
        client.delete_barcode("42")

        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        assert kwargs["data"] == {"button_delete": "42"}

    def test_http_error_raises(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.post.side_effect = requests.RequestException("500")

        client = BarcodeBuddyClient("https://bb.example.com", "key", session=session)
        with pytest.raises(BarcodeBuddyError, match="Failed to delete"):
            client.delete_barcode("42")


# ---------------------------------------------------------------------------
# API key header
# ---------------------------------------------------------------------------

class TestAuth:
    def test_api_key_header_set(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        BarcodeBuddyClient("https://bb.example.com", "my-secret-key", session=session)
        assert session.headers.get("BBUDDY-API-KEY") == "my-secret-key"
