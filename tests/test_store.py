from datetime import UTC, datetime

import pytest

from momo_watch.models import Availability, Event, EventKind, Snapshot, Watch
from momo_watch.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def snap(code="123", price=1000, availability=Availability.IN_STOCK):
    return Snapshot(
        code=code,
        name="測試商品",
        price=price,
        availability=availability,
        fetched_at=datetime.now(UTC),
    )


class TestWatches:
    def test_add_and_list(self, store):
        store.add_watch("123", label="PS5", price_threshold=13000)
        watches = store.list_watches()
        assert len(watches) == 1
        assert watches[0].code == "123"
        assert watches[0].label == "PS5"
        assert watches[0].price_threshold == 13000

    def test_add_twice_updates_instead_of_duplicating(self, store):
        store.add_watch("123", label="舊名稱", price_threshold=1000)
        store.add_watch("123", label="新名稱")
        watches = store.list_watches()
        assert len(watches) == 1
        assert watches[0].label == "新名稱"
        # 沒指定的欄位要保留，不能被 None 洗掉
        assert watches[0].price_threshold == 1000

    def test_remove(self, store):
        store.add_watch("123")
        assert store.remove_watch("123") is True
        assert store.list_watches() == []
        assert store.remove_watch("123") is False

    def test_remove_clears_state(self, store):
        store.add_watch("123")
        store.save_state(snap())
        store.remove_watch("123")
        assert store.get_state("123") is None


class TestState:
    def test_roundtrip(self, store):
        store.add_watch("123")
        original = snap(price=888, availability=Availability.OUT_OF_STOCK)
        store.save_state(original)

        loaded = store.get_state("123")
        assert loaded is not None
        assert loaded.price == 888
        assert loaded.availability is Availability.OUT_OF_STOCK
        assert loaded.name == "測試商品"

    def test_missing_state_is_none(self, store):
        assert store.get_state("nope") is None

    def test_save_overwrites(self, store):
        store.add_watch("123")
        store.save_state(snap(price=1000))
        store.save_state(snap(price=800))
        assert store.get_state("123").price == 800


class TestEvents:
    def test_record_and_read_back(self, store):
        store.add_watch("123")
        watch = Watch(code="123")
        current = snap()
        store.record_event(
            Event(
                kind=EventKind.RESTOCK,
                watch=watch,
                snapshot=current,
                previous=None,
                detail="補貨了",
            )
        )
        rows = store.recent_events()
        assert len(rows) == 1
        assert rows[0]["kind"] == "restock"
        assert rows[0]["detail"] == "補貨了"
        assert rows[0]["price"] == 1000
