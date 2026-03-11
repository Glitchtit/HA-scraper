"""Barcode Buddy client for interacting with pending barcodes.

Barcode Buddy does not expose REST API endpoints for listing or deleting
pending barcodes.  This client scrapes the main web UI HTML to extract them
and uses form POSTs to remove individual entries.

Barcode Buddy has two relevant sections on its main page:

* **New Barcodes** (form ``f1``): barcodes that were looked up and have a
  product name, but have not yet been assigned to a Grocy product.
* **Unknown Barcodes** (form ``f2``): barcodes that could not be resolved to
  any product name at all.

Both sections are scraped by this client.

Authentication is done via the ``BBUDDY-API-KEY`` HTTP header (same as the
official ``/api`` endpoints).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class BarcodeBuddyError(Exception):
    """Raised when a Barcode Buddy request fails."""


@dataclass
class PendingBarcode:
    """A pending barcode entry from Barcode Buddy (new or unknown)."""

    id: str
    barcode: str
    amount: str
    name: str = ""


# Keep the old name as an alias for backwards compatibility.
UnknownBarcode = PendingBarcode


class BarcodeBuddyClient:
    """Client for a Barcode Buddy instance.

    Parameters
    ----------
    base_url:
        Root URL of the Barcode Buddy instance (e.g. ``"https://bb.example.com"``).
    api_key:
        A valid Barcode Buddy API key.
    session:
        Optional :class:`requests.Session` for testing.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._session.headers.update({"BBUDDY-API-KEY": api_key})

    def get_pending_barcodes(self) -> list[PendingBarcode]:
        """Fetch all pending barcodes (new + unknown) from Barcode Buddy.

        Scrapes the main web UI page and parses both the "New Barcodes"
        (form ``f1``) and "Unknown Barcodes" (form ``f2``) tables.
        """
        html = self._fetch_index()
        new = self._parse_new_barcodes(html)
        unknown = self._parse_unknown_barcodes(html)
        return new + unknown

    def get_unknown_barcodes(self) -> list[PendingBarcode]:
        """Fetch only the unknown barcodes (form ``f2``)."""
        return self._parse_unknown_barcodes(self._fetch_index())

    def get_new_barcodes(self) -> list[PendingBarcode]:
        """Fetch only the new/known barcodes (form ``f1``)."""
        return self._parse_new_barcodes(self._fetch_index())

    def delete_barcode(self, barcode_id: str) -> None:
        """Remove a barcode entry by its internal ID.

        Sends a form POST to the Barcode Buddy main page, mimicking the
        "Remove" button in the web UI.
        """
        url = f"{self.base_url}/index.php"
        try:
            resp = self._session.post(
                url,
                data={"button_delete": barcode_id},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BarcodeBuddyError(
                f"Failed to delete barcode {barcode_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_index(self) -> str:
        """GET the Barcode Buddy main page and return the HTML."""
        url = f"{self.base_url}/index.php"
        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            raise BarcodeBuddyError(
                f"Failed to fetch Barcode Buddy page: {exc}"
            ) from exc

    @staticmethod
    def _parse_unknown_barcodes(html: str) -> list[PendingBarcode]:
        """Extract unknown barcodes from form ``f2``.

        Unknown barcodes table columns: Barcode, Look up, Quantity, Product,
        Action, Create, Remove.
        """
        results: list[PendingBarcode] = []

        section = re.search(
            r'id="f2"(.*?)(?:</form>|$)', html, re.DOTALL
        )
        if not section:
            return results

        section_html = section.group(1)

        row_pattern = re.compile(
            r'<tr[^>]*>\s*<td[^>]*>([^<]+)</td>'  # barcode
            r'.*?'  # skip lookup link
            r'<td[^>]*>([^<]*)</td>'  # quantity
            r'.*?'  # skip product select, action buttons, create button
            r'name="button_delete"[^>]*value="(\d+)"',  # id
            re.DOTALL,
        )

        for match in row_pattern.finditer(section_html):
            barcode = match.group(1).strip()
            amount = match.group(2).strip()
            barcode_id = match.group(3).strip()
            results.append(
                PendingBarcode(id=barcode_id, barcode=barcode, amount=amount)
            )

        return results

    @staticmethod
    def _parse_new_barcodes(html: str) -> list[PendingBarcode]:
        """Extract new (known) barcodes from form ``f1``.

        New barcodes table columns: Name, [Federation], Barcode, Quantity,
        Product, Action, Tags, Create, Remove.  The Name is the first <td>.
        """
        results: list[PendingBarcode] = []

        section = re.search(
            r'id="f1"(.*?)(?:</form>|$)', html, re.DOTALL
        )
        if not section:
            return results

        section_html = section.group(1)

        # Each row starts with the product name, then optionally a federation
        # cell, then the barcode.  We match name in the first <td>, then scan
        # forward for a <td> containing a numeric-looking barcode (EAN-8/13),
        # then quantity, then button_delete.
        row_pattern = re.compile(
            r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>'  # name (may contain <span>)
            r'.*?'  # skip optional federation cell
            r'<td[^>]*>(\d{8,14})</td>'  # barcode (EAN-8 to EAN-14)
            r'\s*<td[^>]*>([^<]*)</td>'  # quantity
            r'.*?'  # skip product select, action, tags, create
            r'name="button_delete"[^>]*value="(\d+)"',  # id
            re.DOTALL,
        )

        for match in row_pattern.finditer(section_html):
            raw_name = match.group(1).strip()
            # Strip HTML tags from name (BB wraps long names in <span>).
            name = re.sub(r'<[^>]+>', '', raw_name).strip()
            barcode = match.group(2).strip()
            amount = match.group(3).strip()
            barcode_id = match.group(4).strip()
            results.append(
                PendingBarcode(
                    id=barcode_id, barcode=barcode, amount=amount, name=name
                )
            )

        return results
