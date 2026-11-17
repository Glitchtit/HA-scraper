"""Tests for create_in_storage: the one create sequence every caller shares."""

from __future__ import annotations

from argparse import Namespace
from unittest import mock

import pytest
import requests

import ingress_server
import main
from scraper.scraper import KRuokaScraper, Product
from scraper.storage_client import StorageAPIError, create_in_storage

from .test_scraper_services import services

EAN = "6410405082657"
FOUND = Product(
    name="Pirkka kevytmaito 1l", ean=EAN, description="Maito",
    image_url=f"https://img.example/{EAN}",
)
# The writes a new product takes, in order, whoever asks for it.
WRITES = [
    ("create_product", ("Pirkka kevytmaito 1l",),
     {"description": "Maito", "location_id": None, "unit_id": None}),
    ("add_barcode", (1, EAN), {}),
    ("upload_product_image", (1, f"{EAN}.jpg"), {}),
]


def _writes(storage) -> list[tuple]:
    return [call for call in storage.calls if not call[0].startswith("get_")]


class TestCreateInStorage:
    def test_creates_product_barcode_and_image(self, world):
        product_id = create_in_storage(world.storage, FOUND, ean=EAN)

        assert product_id == 1
        assert _writes(world.storage) == WRITES
        product = world.storage.products[1]
        assert product["name"] == "Pirkka kevytmaito 1l"
        assert product["description"] == "Maito"
        assert product["picture_filename"] == f"{EAN}.jpg"
        assert world.storage.barcodes == {EAN: 1}
        assert world.downloads == [FOUND.image_url]

    def test_existing_barcode_returns_none_and_writes_nothing(self, world):
        world.storage.add_product("Maito", barcode=EAN)

        assert create_in_storage(world.storage, FOUND, ean=EAN) is None
        assert _writes(world.storage) == []

    def test_barcode_failure_leaves_no_product(self, world):
        world.storage.failing["add_barcode"] = StorageAPIError("refused")

        with pytest.raises(StorageAPIError):
            create_in_storage(world.storage, FOUND, ean=EAN)

        assert world.storage.products == {}
        assert world.storage.called("delete_product") == [((1,), {})]
        assert world.downloads == []

    def test_price_is_set_when_given(self, world):
        create_in_storage(world.storage, FOUND, ean=EAN, price=1.35)

        product = world.storage.products[1]
        assert (product["unit_price"], product["unit_price_currency"]) == (1.35, "EUR")

    def test_no_price_fields_without_a_price(self, world):
        create_in_storage(world.storage, FOUND, ean=EAN)

        assert "unit_price_currency" not in world.storage.products[1]

    def test_parent_and_group_are_set_when_given(self, world):
        create_in_storage(
            world.storage, FOUND, ean=EAN, parent_id=7, product_group_id=3,
        )

        product = world.storage.products[1]
        assert (product["parent_id"], product["product_group_id"]) == (7, 3)

    def test_availability_is_written(self, world):
        entries = [{"store_id": "K532", "available": True}]

        create_in_storage(world.storage, FOUND, ean=EAN, availability=entries)

        assert world.storage.availability == {1: entries}

    def test_failed_availability_write_keeps_the_product(self, world):
        world.storage.failing["set_product_availability"] = StorageAPIError("down")

        product_id = create_in_storage(
            world.storage, FOUND, ean=EAN,
            availability=[{"store_id": "K532", "available": True}],
        )

        assert product_id == 1
        assert 1 in world.storage.products

    def test_failed_image_download_keeps_the_product(self, world, monkeypatch):
        def refuse(url, timeout=None):
            raise requests.ConnectionError("no route")

        monkeypatch.setattr(requests, "get", refuse)

        assert create_in_storage(world.storage, FOUND, ean=EAN) == 1
        assert world.storage.products[1]["picture_filename"] is None

    def test_product_without_barcode_gets_no_barcode_and_its_own_image_name(self, world):
        found = Product(name="Kotitekoinen pulla", ean="", image_url="https://img.example/pulla")

        product_id = create_in_storage(world.storage, found, ean="")

        assert world.storage.barcodes == {}
        assert world.storage.called("get_product_by_barcode") == []
        assert world.storage.products[product_id]["picture_filename"] == "product-1.jpg"


class TestEveryCallerMakesTheSameWrites:
    def test_sync_product(self, world):
        product_id = main.sync_product(FOUND, world.storage)

        assert product_id == 1
        assert _writes(world.storage) == WRITES

    def test_add_products_handler(self, world):
        opts = {"storage_url": "http://storage"}
        with mock.patch.object(ingress_server, "_read_options", return_value=opts):
            result = ingress_server._handle_add_products({"products": [{
                "name": FOUND.name, "ean": EAN,
                "description": FOUND.description, "image_url": FOUND.image_url,
            }]})

        assert result["added_ids"] == [1]
        assert _writes(world.storage) == WRITES

    def test_integration_add_product_service(self, world, monkeypatch):
        monkeypatch.setattr(
            "scraper_integration.scraperlib.storage_client.StorageClient",
            lambda base_url: world.storage,
        )

        product_id = services._add_product_sync_worker("http://storage", {
            "name": FOUND.name, "ean": EAN,
            "description": FOUND.description, "image_url": FOUND.image_url,
        })

        assert product_id == 1
        assert _writes(world.storage) == WRITES


class TestQueueDiscover:
    def _args(self):
        return Namespace(
            store="K532", storage_url="http://storage", upload_images=False,
            searxng_url="",
        )

    def test_queue_discover_sets_the_price_like_a_single_scan(self, world, monkeypatch):
        monkeypatch.setenv("FLARESOLVERR_URL", "http://fs:8191")
        world.kruoka.catalogue = {"K532": {EAN: "Pirkka kevytmaito 1l"}}
        world.kruoka.prices = {EAN: 1.35}
        world.storage.enqueue(EAN)

        rc, _ = main._discover_products(self._args())

        assert rc == 0
        assert world.storage.products[world.storage.barcodes[EAN]]["unit_price"] == 1.35

    def test_stock_is_not_added_twice_when_done_could_not_be_recorded(self, world):
        world.kruoka.catalogue = {"K532": {EAN: "Pirkka kevytmaito 1l"}}
        world.storage.enqueue(EAN)
        world.storage.failing["update_barcode_queue_item"] = StorageAPIError("down")

        main._discover_products(self._args())

        assert world.storage.queue[0]["status"] == "pending"

        del world.storage.failing["update_barcode_queue_item"]
        rc, _ = main._discover_products(self._args())

        assert rc == 0
        assert world.storage.queue[0]["status"] == "done"
        assert world.storage.stock == {world.storage.barcodes[EAN]: 1.0}

    def test_storage_failing_for_one_item_does_not_abort_the_run(self, world):
        world.kruoka.catalogue = {"K532": {EAN: "Pirkka kevytmaito 1l", "111": "Voi"}}
        world.storage.enqueue(EAN)
        world.storage.enqueue("111")
        world.storage.failing["get_product_by_barcode"] = StorageAPIError("down")

        rc, _ = main._discover_products(self._args())

        assert rc == 1
        # Both were tried; each records its own error, as before.
        assert [i["status"] for i in world.storage.queue] == ["error", "error"]
        assert world.storage.called("create_product") == []
