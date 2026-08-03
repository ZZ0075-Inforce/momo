"""安全閘門與 PurchaseHandler。

check_guards 是純函式所以單獨測；PurchaseHandler 則跑真的瀏覽器打假商店。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from momo_watch.flow import Guards, parse_flow
from momo_watch.models import Availability, Event, EventKind, Snapshot, Watch
from momo_watch.purchase import Outcome, PurchaseHandler, check_guards
from momo_watch.store import Store

pytest.importorskip("playwright.async_api")


def make_event(kind=EventKind.RESTOCK, price=1299, code="6453015") -> Event:
    snapshot = Snapshot(
        code=code,
        name="測試商品",
        price=price,
        availability=Availability.IN_STOCK,
        fetched_at=datetime.now(UTC),
    )
    return Event(
        kind=kind,
        watch=Watch(code=code, label="測試商品"),
        snapshot=snapshot,
        previous=None,
        detail="補貨了",
    )


class TestGuards:
    def test_allows_a_clean_event(self):
        verdict = check_guards(
            Guards(max_price=15000), make_event(price=1299), attempts=0, has_placeholders=False
        )
        assert verdict.allowed

    def test_blocks_when_placeholders_remain(self):
        verdict = check_guards(Guards(), make_event(), attempts=0, has_placeholders=True)
        assert not verdict.allowed
        assert "TODO" in verdict.reason

    def test_blocks_above_price_ceiling(self):
        verdict = check_guards(
            Guards(max_price=1000), make_event(price=1299), attempts=0, has_placeholders=False
        )
        assert not verdict.allowed
        assert "超過上限" in verdict.reason

    def test_allows_exactly_at_ceiling(self):
        verdict = check_guards(
            Guards(max_price=1299), make_event(price=1299), attempts=0, has_placeholders=False
        )
        assert verdict.allowed

    def test_unknown_price_fails_closed(self):
        """設了價格上限卻讀不到價格 —— 必須擋下，不能當作沒設限。

        解析壞掉時最不該做的事就是照買。
        """
        verdict = check_guards(
            Guards(max_price=15000), make_event(price=None), attempts=0, has_placeholders=False
        )
        assert not verdict.allowed
        assert "解析不到價格" in verdict.reason

    def test_unknown_price_allowed_when_no_ceiling_set(self):
        verdict = check_guards(
            Guards(max_price=None), make_event(price=None), attempts=0, has_placeholders=False
        )
        assert verdict.allowed

    def test_blocks_after_max_attempts(self):
        verdict = check_guards(
            Guards(max_attempts=1), make_event(), attempts=1, has_placeholders=False
        )
        assert not verdict.allowed
        assert "嘗試上限" in verdict.reason

    def test_allows_within_max_attempts(self):
        verdict = check_guards(
            Guards(max_attempts=3), make_event(), attempts=2, has_placeholders=False
        )
        assert verdict.allowed


class RecordingSender:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "purchase.db")
    yield s
    s.close()


def shop_flow(base: str, **guards):
    return parse_flow(
        {
            "guards": guards,
            "steps": [
                {
                    "name": "開啟商品頁",
                    "action": "goto",
                    "url": f"{base}/product.html?code={{code}}",
                },
                {
                    "name": "加入購物車",
                    "action": "click",
                    "selector": "#add-cart",
                    "mutating": True,
                },
                {"name": "等待加入成功", "action": "wait_for", "selector": "#cart-added"},
                {"name": "前往購物車", "action": "goto", "url": f"{base}/cart.html"},
                {"name": "前往結帳", "action": "click", "selector": "#checkout", "mutating": True},
                {"name": "確認已到付款頁", "action": "expect", "selector": "#payment-form"},
            ],
        }
    )


def make_handler(flow, browser_session, store, sender, tmp_path):
    return PurchaseHandler(flow, browser_session, store, sender, screenshot_dir=tmp_path / "shots")


class TestPurchaseHandler:
    async def test_non_urgent_events_are_ignored(self, fake_shop, browser_session, store, tmp_path):
        sender = RecordingSender()
        handler = make_handler(shop_flow(fake_shop), browser_session, store, sender, tmp_path)

        await handler.handle(make_event(kind=EventKind.PRICE_RISE))

        assert sender.messages == []
        assert store.recent_attempts() == []

    async def test_runs_full_flow_and_stops_at_payment(
        self, fake_shop, browser_session, store, tmp_path
    ):
        sender = RecordingSender()
        flow = shop_flow(fake_shop, dry_run=False, max_price=15000)
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event())

        attempts = store.recent_attempts()
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == Outcome.READY_FOR_PAYMENT
        assert attempts[0]["reached"] == "確認已到付款頁"
        assert "手動完成最後一步" in sender.messages[0]

    async def test_dry_run_stops_early_and_records_as_dry(
        self, fake_shop, browser_session, store, tmp_path
    ):
        sender = RecordingSender()
        flow = shop_flow(fake_shop, dry_run=True)
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event())

        attempt = store.recent_attempts()[0]
        assert attempt["dry_run"] == 1
        assert "演練" in sender.messages[0]

    async def test_dry_run_does_not_consume_the_attempt_budget(
        self, fake_shop, browser_session, store, tmp_path
    ):
        """演練不會真的下單，不該吃掉 max_attempts 的額度。"""
        sender = RecordingSender()
        flow = shop_flow(fake_shop, dry_run=True, max_attempts=1)
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event())
        await handler.handle(make_event())

        assert store.count_attempts("6453015") == 0
        assert len(store.recent_attempts()) == 2

    async def test_second_real_attempt_is_blocked(
        self, fake_shop, browser_session, store, tmp_path
    ):
        """補貨事件連續觸發時，只該真的跑一次。"""
        sender = RecordingSender()
        flow = shop_flow(fake_shop, dry_run=False, max_attempts=1)
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event())
        await handler.handle(make_event())

        outcomes = [row["outcome"] for row in store.recent_attempts()]
        assert outcomes.count(Outcome.BLOCKED) == 1
        assert outcomes.count(Outcome.READY_FOR_PAYMENT) == 1

    async def test_placeholders_block_execution(self, fake_shop, browser_session, store, tmp_path):
        sender = RecordingSender()
        flow = parse_flow(
            {
                "guards": {"dry_run": False},
                "steps": [{"name": "加入購物車", "action": "click", "selector": "TODO_按鈕"}],
            }
        )
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event())

        assert store.recent_attempts()[0]["outcome"] == Outcome.BLOCKED
        assert "TODO" in sender.messages[0]

    async def test_price_ceiling_blocks_execution(
        self, fake_shop, browser_session, store, tmp_path
    ):
        sender = RecordingSender()
        flow = shop_flow(fake_shop, dry_run=False, max_price=500)
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event(price=1299))

        assert store.recent_attempts()[0]["outcome"] == Outcome.BLOCKED
        assert "超過上限" in sender.messages[0]

    async def test_failure_is_recorded_with_the_failing_step(
        self, fake_shop, browser_session, store, tmp_path
    ):
        sender = RecordingSender()
        flow = parse_flow(
            {
                "guards": {"dry_run": False},
                "steps": [
                    {"name": "開啟商品頁", "action": "goto", "url": f"{fake_shop}/product.html"},
                    {
                        "name": "點一個不存在的鈕",
                        "action": "click",
                        "selector": "#nope",
                        "timeout_ms": 800,
                    },
                ],
            }
        )
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event())

        attempt = store.recent_attempts()[0]
        assert attempt["outcome"] == Outcome.FAILED
        assert attempt["reached"] == "開啟商品頁"
        assert "卡在" in sender.messages[0]

    async def test_screenshot_is_captured_on_failure(
        self, fake_shop, browser_session, store, tmp_path
    ):
        """失敗時的截圖是唯一能看出頁面長怎樣的線索，一定要留下來。"""
        sender = RecordingSender()
        flow = parse_flow(
            {
                "guards": {"dry_run": False},
                "steps": [
                    {"name": "開啟商品頁", "action": "goto", "url": f"{fake_shop}/product.html"},
                    {"name": "壞掉", "action": "click", "selector": "#nope", "timeout_ms": 800},
                ],
            }
        )
        handler = make_handler(flow, browser_session, store, sender, tmp_path)

        await handler.handle(make_event())

        shots = list((tmp_path / "shots").glob("6453015-*.png"))
        assert len(shots) == 1
        assert shots[0].stat().st_size > 0
