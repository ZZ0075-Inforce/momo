"""端到端測試：Watcher + Store + handler 的接線。

用 stub client 取代真正的 HTTP，所以完全離線，也不會打擾 momo。
這是唯一會驗證「整套接起來真的會動」的測試。
"""

from datetime import UTC, datetime

import pytest

from momo_watch.client import FetchError
from momo_watch.config import Config
from momo_watch.models import Availability, Event, EventKind, Snapshot
from momo_watch.store import Store
from momo_watch.watcher import Watcher


class StubClient:
    """照著腳本回傳快照；腳本元素若是 Exception 就丟出來。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def fetch(self, code: str) -> Snapshot:
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        availability, price = item
        return Snapshot(
            code=code,
            name="測試商品",
            price=price,
            availability=availability,
            fetched_at=datetime.now(UTC),
        )


class RecordingHandler:
    def __init__(self):
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


class ExplodingHandler:
    async def handle(self, event: Event) -> None:
        raise RuntimeError("handler 壞掉了")


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        poll_interval=5.0,
        poll_jitter=0.0,
        concurrency=2,
        min_request_gap=0.0,
        http_timeout=5.0,
        telegram_token=None,
        telegram_chat_id=None,
        log_level="CRITICAL",
    )


@pytest.fixture
def store(config):
    s = Store(config.db_path)
    yield s
    s.close()


def build(config, store, client, handlers):
    return Watcher(config, store, client, handlers)


async def test_restock_flows_end_to_end(config, store):
    """缺貨 → 有貨：事件要送到 handler，也要落進資料庫。"""
    store.add_watch("6453015", label="PS5")
    handler = RecordingHandler()
    client = StubClient(
        [
            (Availability.OUT_OF_STOCK, 13980),
            (Availability.IN_STOCK, 13980),
        ]
    )
    watcher = build(config, store, client, [handler])

    await watcher.run_once()  # 首次
    await watcher.run_once()  # 補貨

    kinds = [e.kind for e in handler.events]
    assert kinds == [EventKind.FIRST_SEEN, EventKind.RESTOCK]
    assert handler.events[-1].is_urgent

    persisted = {row["kind"] for row in store.recent_events()}
    assert persisted == {"first_seen", "restock"}
    assert store.get_state("6453015").availability is Availability.IN_STOCK


async def test_transient_failure_does_not_lose_the_restock(config, store):
    """中間插一次抓取失敗，補貨訊號仍然要報出來。"""
    store.add_watch("6453015")
    handler = RecordingHandler()
    client = StubClient(
        [
            (Availability.OUT_OF_STOCK, 13980),
            FetchError("模擬逾時"),
            (Availability.IN_STOCK, 13980),
        ]
    )
    watcher = build(config, store, client, [handler])

    for _ in range(3):
        await watcher.run_once()

    assert [e.kind for e in handler.events] == [EventKind.FIRST_SEEN, EventKind.RESTOCK]


async def test_unknown_parse_does_not_fake_a_restock(config, store):
    """momo 改版導致解析不到（UNKNOWN），不該憑空生出補貨事件。"""
    store.add_watch("6453015")
    handler = RecordingHandler()
    client = StubClient(
        [
            (Availability.OUT_OF_STOCK, 13980),
            (Availability.UNKNOWN, None),
            (Availability.UNKNOWN, None),
        ]
    )
    watcher = build(config, store, client, [handler])

    for _ in range(3):
        await watcher.run_once()

    assert EventKind.RESTOCK not in {e.kind for e in handler.events}


async def test_threshold_notifies_once(config, store):
    store.add_watch("6453015", price_threshold=13000)
    handler = RecordingHandler()
    client = StubClient(
        [
            (Availability.IN_STOCK, 13980),
            (Availability.IN_STOCK, 12800),
            (Availability.IN_STOCK, 12500),
        ]
    )
    watcher = build(config, store, client, [handler])

    for _ in range(3):
        await watcher.run_once()

    below = [e for e in handler.events if e.kind is EventKind.BELOW_THRESHOLD]
    assert len(below) == 1, "跌破目標價只該通知一次，不是每輪都吵"


async def test_broken_handler_does_not_stop_the_others(config, store):
    """一個 handler 爆掉不能讓其他 handler 收不到 —— Phase 2 會掛上下單 handler。"""
    store.add_watch("6453015")
    handler = RecordingHandler()
    client = StubClient([(Availability.IN_STOCK, 13980)])
    watcher = build(config, store, client, [ExplodingHandler(), handler])

    await watcher.run_once()

    assert [e.kind for e in handler.events] == [EventKind.FIRST_SEEN]


async def test_multiple_watches_are_checked_concurrently(config, store):
    store.add_watch("111")
    store.add_watch("222")
    handler = RecordingHandler()
    client = StubClient([(Availability.IN_STOCK, 1000)])
    watcher = build(config, store, client, [handler])

    await watcher.run_once()

    assert client.calls == 2
    assert {e.snapshot.code for e in handler.events} == {"111", "222"}


async def test_empty_watchlist_is_a_noop(config, store):
    handler = RecordingHandler()
    watcher = build(config, store, StubClient([]), [handler])
    assert await watcher.run_once() == []
    assert handler.events == []
