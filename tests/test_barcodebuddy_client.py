"""Tests for BarcodeBuddyClient."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from grocy_scraper.barcodebuddy_client import (
    BarcodeBuddyClient,
    BarcodeBuddyError,
    PendingBarcode,
    UnknownBarcode,
)


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

# Minimal HTML matching Barcode Buddy's "New Barcodes" (f1) table structure.
_HTML_WITH_NEW_BARCODES = """
<html><body>
<form id="f1" method="post">
<table>
<tr><th>Name</th><th>Barcode</th><th>Quantity</th><th>Product</th>
<th>Action</th><th>Tags</th><th>Create</th><th>Remove</th></tr>
<tr>
  <td><span>Pirkka kevytmaito 1l</span></td>
  <td>6410405082657</td>
  <td>2</td>
  <td><select id="select_10" name="select_10"><option>--</option></select></td>
  <td><button name="button_add" value="10">Add</button></td>
  <td>checkbox stuff</td>
  <td><button name="button_createproduct">Create</button></td>
  <td><button name="button_delete" value="10">Remove</button></td>
</tr>
</table>
</form>
<form id="f2" method="post">
<table>
<tr><th>Barcode</th><th>Look up</th><th>Quantity</th><th>Product</th>
<th>Action</th><th>Create</th><th>Remove</th></tr>
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

# Only unknown barcodes (f2), no new barcodes (f1).
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
        assert result[0] == PendingBarcode(id="42", barcode="6410405082657", amount="1")
        assert result[1] == PendingBarcode(id="99", barcode="5901234123457", amount="3")

    def test_no_unknown_barcodes(self):
        result = BarcodeBuddyClient._parse_unknown_barcodes(_HTML_NO_UNKNOWNS)
        assert result == []

    def test_no_f2_section(self):
        result = BarcodeBuddyClient._parse_unknown_barcodes(_HTML_NO_SECTION)
        assert result == []


# ---------------------------------------------------------------------------
# _parse_new_barcodes (static method, no HTTP)
# ---------------------------------------------------------------------------

class TestParseNewBarcodes:
    def test_parses_new_barcode_with_name(self):
        result = BarcodeBuddyClient._parse_new_barcodes(_HTML_WITH_NEW_BARCODES)
        assert len(result) == 1
        assert result[0].barcode == "6410405082657"
        assert result[0].name == "Pirkka kevytmaito 1l"
        assert result[0].amount == "2"
        assert result[0].id == "10"

    def test_no_f1_section(self):
        result = BarcodeBuddyClient._parse_new_barcodes(_HTML_NO_SECTION)
        assert result == []

    def test_no_new_barcodes_in_unknowns_only_html(self):
        result = BarcodeBuddyClient._parse_new_barcodes(_HTML_WITH_UNKNOWNS)
        assert result == []


# ---------------------------------------------------------------------------
# get_pending_barcodes (combines new + unknown)
# ---------------------------------------------------------------------------

class TestGetPendingBarcodes:
    def test_combines_new_and_unknown(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        resp = MagicMock()
        resp.text = _HTML_WITH_NEW_BARCODES
        resp.raise_for_status.return_value = None
        session.get.return_value = resp

        client = BarcodeBuddyClient("https://bb.example.com", "key", session=session)
        pending = client.get_pending_barcodes()

        # 1 new + 1 unknown in _HTML_WITH_NEW_BARCODES
        assert len(pending) == 2
        # New barcode has a name.
        assert pending[0].name == "Pirkka kevytmaito 1l"
        assert pending[0].barcode == "6410405082657"
        # Unknown barcode has no name.
        assert pending[1].name == ""
        assert pending[1].barcode == "5901234123457"


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
# Backwards compatibility alias
# ---------------------------------------------------------------------------

class TestAlias:
    def test_unknown_barcode_is_pending_barcode(self):
        assert UnknownBarcode is PendingBarcode


# ---------------------------------------------------------------------------
# API key header
# ---------------------------------------------------------------------------

class TestAuth:
    def test_api_key_header_set(self):
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        BarcodeBuddyClient("https://bb.example.com", "my-secret-key", session=session)
        assert session.headers.get("BBUDDY-API-KEY") == "my-secret-key"
