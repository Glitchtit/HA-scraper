"""Tests for resolve_ean: one lookup cascade that tells failure from absence."""

from __future__ import annotations

import pytest

from scraper.lookup import LookupFailed, resolve_ean
from scraper.scraper import KRuokaScraper
from scraper.searxng_client import SearXNGProduct
from scraper.skaupat_client import SKaupatProduct

EAN = "6410405082657"
SEARXNG = "http://searxng:8080"


def _scrapers(*store_ids: str) -> list[KRuokaScraper]:
    return [KRuokaScraper(store_id=s, request_delay=0) for s in store_ids]


class TestResolveEan:
    def test_found_on_kruoka(self, world):
        world.kruoka.catalogue = {"K532": {EAN: "Pirkka kevytmaito 1l"}}

        found = resolve_ean(EAN, _scrapers("K532"), searxng_url=SEARXNG)

        assert (found.name, found.ean) == ("Pirkka kevytmaito 1l", EAN)
        assert world.asked == []  # no fallback source was needed

    def test_found_in_a_later_store(self, world):
        world.kruoka.catalogue = {"N110": {EAN: "Pirkka kevytmaito 1l"}}

        found = resolve_ean(EAN, _scrapers("K532", "N110"))

        assert found.name == "Pirkka kevytmaito 1l"
        assert world.kruoka.asked("graphql") == [("K532", EAN), ("N110", EAN)]

    def test_all_sources_answering_none_returns_none(self, world):
        assert resolve_ean(EAN, _scrapers("K532"), searxng_url=SEARXNG) is None
        assert world.asked == [("skaupat", EAN), ("searxng", EAN)]

    def test_all_sources_failing_raises(self, world):
        world.kruoka.down = True
        world.down |= {"skaupat", "searxng"}

        with pytest.raises(LookupFailed, match="K-Ruoka"):
            resolve_ean(EAN, _scrapers("K532"), searxng_url=SEARXNG)

    def test_kruoka_failing_and_skaupat_finding_returns_skaupat_product(self, world):
        world.kruoka.down = True
        world.skaupat[EAN] = SKaupatProduct(
            name="Kotimaista kevytmaito 1l", ean=EAN, description="Maito",
            image_url="https://cdn.example/kuva1.webp",
        )

        found = resolve_ean(EAN, _scrapers("K532"))

        assert found.name == "Kotimaista kevytmaito 1l"
        assert found.description == "Maito"
        assert found.image_url == "https://cdn.example/kuva1.webp"

    def test_kruoka_failing_and_nothing_found_raises_without_asking_searxng(self, world):
        """A web-search guess must not stand in for the catalogue during an
        outage, least of all the nameless "Unknown product" one."""
        world.kruoka.down = True
        world.searxng[EAN] = SearXNGProduct(name=f"Unknown product ({EAN})", ean=EAN)

        with pytest.raises(LookupFailed):
            resolve_ean(EAN, _scrapers("K532"), searxng_url=SEARXNG)
        assert ("searxng", EAN) not in world.asked

    def test_one_store_failing_is_a_failed_lookup_unless_another_has_it(self, world):
        scrapers = _scrapers("K532", "N110")
        scrapers[0]._session = type(world.kruoka)()  # its own, unreachable K-Ruoka
        scrapers[0]._session.down = True

        with pytest.raises(LookupFailed):
            resolve_ean(EAN, scrapers)

        world.kruoka.catalogue = {"N110": {EAN: "Pirkka kevytmaito 1l"}}
        assert resolve_ean(EAN, scrapers).name == "Pirkka kevytmaito 1l"

    def test_searxng_stands_in_when_the_stores_answered_none(self, world):
        world.searxng[EAN] = SearXNGProduct(
            name="Capsi merisuolamylly 230g", ean=EAN,
            image_url="https://img.example/x", source_url="https://www.k-ruoka.fi/x",
        )

        found = resolve_ean(EAN, _scrapers("K532"), searxng_url=SEARXNG)

        assert (found.name, found.image_url) == ("Capsi merisuolamylly 230g", "https://img.example/x")

    def test_searxng_is_not_asked_without_a_url(self, world):
        world.searxng[EAN] = SearXNGProduct(name="X", ean=EAN)

        assert resolve_ean(EAN, _scrapers("K532")) is None
        assert world.asked == [("skaupat", EAN)]

    def test_a_failing_fallback_alone_is_not_a_failed_lookup(self, world):
        """K-Ruoka answered: S-kaupat or SearXNG being down must not turn
        every product K-Ruoka does not carry into "lookup failed"."""
        world.down |= {"skaupat", "searxng"}

        assert resolve_ean(EAN, _scrapers("K532"), searxng_url=SEARXNG) is None


class TestGraphqlFailuresAreCounted:
    """The caller can tell "K-Ruoka has no such product" from "no answer"."""

    def test_empty_result_is_not_a_failure(self, world):
        scraper = KRuokaScraper(store_id="K532")
        assert list(scraper.search(EAN)) == []
        assert scraper.failed_requests == 0

    def test_unreachable_is_a_failure(self, world):
        world.kruoka.down = True
        scraper = KRuokaScraper(store_id="K532")
        assert list(scraper.search(EAN)) == []
        assert scraper.failed_requests == 1
