"""Barcode Buddy client for interacting with unknown barcodes.

Barcode Buddy does not expose REST API endpoints for listing or deleting
unknown barcodes.  This client scrapes the main web UI HTML to extract them
and uses form POSTs to remove individual entries.

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
class UnknownBarcode:
    """An unknown barcode entry from Barcode Buddy."""

    id: str
    barcode: str
    amount: str


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

    def get_unknown_barcodes(self) -> list[UnknownBarcode]:
        """Fetch the list of unknown barcodes from Barcode Buddy.

        Scrapes the main web UI page and parses the "Unknown Barcodes" table.
        """
        url = f"{self.base_url}/index.php"
        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BarcodeBuddyError(
                f"Failed to fetch Barcode Buddy page: {exc}"
            ) from exc

        return self._parse_unknown_barcodes(resp.text)

    def delete_barcode(self, barcode_id: str) -> None:
        """Remove an unknown barcode entry by its internal ID.

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

    @staticmethod
    def _parse_unknown_barcodes(html: str) -> list[UnknownBarcode]:
        """Extract unknown barcodes from the Barcode Buddy HTML page.

        The unknown barcodes table contains rows with:
        - Column 1: barcode string
        - Column 3: quantity
        - A "button_delete" with value=<id>

        We parse the HTML with regex since BB produces simple, predictable markup.
        """
        results: list[UnknownBarcode] = []

        # Find the unknown barcodes card/section.  BB wraps it in a card
        # with heading "Unknown Barcodes".  The table rows contain the data.
        # Each row has a button_delete with value=<id>.
        #
        # Strategy: find all table rows that contain a button_delete, then
        # extract the barcode from the first <td> and the id from the button value.

        # Match table rows within the unknown barcodes section.
        # The section is identified by the "f2" form id used by BB.
        unknown_section = re.search(
            r'id="f2"(.*?)(?:</form>|$)', html, re.DOTALL
        )
        if not unknown_section:
            return results

        section_html = unknown_section.group(1)

        # Find each row: extract barcode (first <td>), quantity, and delete button id.
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
                UnknownBarcode(id=barcode_id, barcode=barcode, amount=amount)
            )

        return results
