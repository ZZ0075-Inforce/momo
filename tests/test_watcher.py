from datetime import UTC, datetime

import pytest

from momo_watch.models import Availability, EventKind, Snapshot, Watch
from momo_watch.watcher import diff, merge_state


def snap(availability=Availability.IN_STOCK, price=1000, code="123", name="測試商品"):
    return Snapshot(
        code=code,
        name=name,
        price=price,
        availability=availability,
        fetched_at=datetime.now(UTC),
    )


WATCH = Watch(code="123")


class TestDiffAvailability:
    def test_first_seen(self):
        events = diff(WATCH, None, snap())
        assert [e.kind for e in events] == [EventKind.FIRST_SEEN]

    def test_restock_is_detected(self):
        events = diff(WATCH, snap(Availability.OUT_OF_STOCK), snap(Availability.IN_STOCK))
        assert EventKind.RESTOCK in {e.kind for e in events}

    def test_sold_out_is_detected(self):
        events = diff(WATCH, snap(Availability.IN_STOCK), snap(Availability.OUT_OF_STOCK))
        assert EventKind.SOLD_OUT in {e.kind for e in events}

    def test_no_event_when_unchanged(self):
        assert diff(WATCH, snap(), snap()) == []

    @pytest.mark.parametrize(
        ("prev", "cur"),
        [
            (Availability.UNKNOWN, Availability.IN_STOCK),
            (Availability.OUT_OF_STOCK, Availability.UNKNOWN),
            (Availability.UNKNOWN, Availability.OUT_OF_STOCK),
        ],
    )
    def test_unknown_never_produces_stock_event(self, prev, cur):
        """UNKNOWN 參與的轉換一律不報，避免一次抓取失敗變成假警報。"""
        kinds = {e.kind for e in diff(WATCH, snap(prev), snap(cur))}
        assert not kinds & {EventKind.RESTOCK, EventKind.SOLD_OUT}


class TestDiffPrice:
    def test_price_drop(self):
        events = diff(WATCH, snap(price=1000), snap(price=800))
        drop = next(e for e in events if e.kind is EventKind.PRICE_DROP)
        assert "200" in drop.detail

    def test_price_rise(self):
        kinds = {e.kind for e in diff(WATCH, snap(price=800), snap(price=1000))}
        assert EventKind.PRICE_RISE in kinds

    def test_missing_price_produces_no_event(self):
        assert diff(WATCH, snap(price=None), snap(price=None)) == []
        assert not {e.kind for e in diff(WATCH, snap(price=1000), snap(price=None))} & {
            EventKind.PRICE_DROP,
            EventKind.PRICE_RISE,
        }


class TestThreshold:
    watch = Watch(code="123", price_threshold=900)

    def test_fires_when_crossing_below(self):
        kinds = {e.kind for e in diff(self.watch, snap(price=1000), snap(price=850))}
        assert EventKind.BELOW_THRESHOLD in kinds

    def test_does_not_refire_while_still_below(self):
        """已經在門檻下就別再吵 —— 每輪都推播會讓你把通知靜音。"""
        kinds = {e.kind for e in diff(self.watch, snap(price=850), snap(price=840))}
        assert EventKind.BELOW_THRESHOLD not in kinds

    def test_fires_again_after_going_back_above(self):
        kinds = {e.kind for e in diff(self.watch, snap(price=950), snap(price=880))}
        assert EventKind.BELOW_THRESHOLD in kinds

    def test_exact_threshold_counts_as_below(self):
        kinds = {e.kind for e in diff(self.watch, snap(price=1000), snap(price=900))}
        assert EventKind.BELOW_THRESHOLD in kinds


class TestMergeState:
    def test_unknown_keeps_previous_availability(self):
        previous = snap(Availability.OUT_OF_STOCK, price=1000)
        merged = merge_state(previous, snap(Availability.UNKNOWN, price=None))
        assert merged.availability is Availability.OUT_OF_STOCK
        assert merged.price == 1000

    def test_known_availability_overwrites(self):
        merged = merge_state(snap(Availability.OUT_OF_STOCK), snap(Availability.IN_STOCK))
        assert merged.availability is Availability.IN_STOCK

    def test_no_previous_state_passes_through(self):
        current = snap(Availability.UNKNOWN)
        assert merge_state(None, current) is current

    def test_transient_failure_does_not_swallow_restock(self):
        """OUT -> (逾時) -> IN 這個序列必須仍然報出補貨。"""
        previous = snap(Availability.OUT_OF_STOCK)
        after_failure = merge_state(previous, snap(Availability.UNKNOWN, price=None))
        events = diff(WATCH, after_failure, snap(Availability.IN_STOCK))
        assert EventKind.RESTOCK in {e.kind for e in events}
