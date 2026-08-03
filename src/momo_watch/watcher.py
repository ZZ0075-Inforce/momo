"""輪詢迴圈與狀態比對。"""

from __future__ import annotations

import asyncio
import logging
import random

from .client import FetchError, MomoClient
from .config import Config
from .models import Availability, Event, EventKind, Snapshot, Watch
from .notifier import Handler
from .store import Store

log = logging.getLogger(__name__)


def merge_state(previous: Snapshot | None, current: Snapshot) -> Snapshot:
    """抓取失敗或改版導致 availability 判不出來時，沿用上一次「已知」的狀態。

    否則會出現這個假訊號序列：
        OUT_OF_STOCK -> UNKNOWN（一次逾時）-> IN_STOCK
    中間那次 UNKNOWN 會把 OUT 蓋掉，等真的抓到 IN 時就比不出「補貨」了。
    """
    if current.availability is not Availability.UNKNOWN or previous is None:
        return current
    return Snapshot(
        code=current.code,
        name=current.name or previous.name,
        price=current.price if current.price is not None else previous.price,
        availability=previous.availability,
        fetched_at=current.fetched_at,
    )


def diff(watch: Watch, previous: Snapshot | None, current: Snapshot) -> list[Event]:
    """比對前後狀態，產生事件。"""

    def make(kind: EventKind, detail: str) -> Event:
        return Event(kind=kind, watch=watch, snapshot=current, previous=previous, detail=detail)

    if previous is None:
        label = "有貨" if current.availability is Availability.IN_STOCK else "無貨／未知"
        return [make(EventKind.FIRST_SEEN, f"開始監控（目前 {label}）")]

    events: list[Event] = []

    # --- 庫存變化。任一邊是 UNKNOWN 就不判斷，寧可漏報也不要誤報。---
    prev_av, cur_av = previous.availability, current.availability
    if Availability.UNKNOWN not in (prev_av, cur_av) and prev_av is not cur_av:
        if cur_av is Availability.IN_STOCK:
            events.append(make(EventKind.RESTOCK, "🔥 補貨了，快去搶"))
        else:
            events.append(make(EventKind.SOLD_OUT, "已售完／下架"))

    # --- 價格變化 ---
    prev_price, cur_price = previous.price, current.price
    if prev_price is not None and cur_price is not None and prev_price != cur_price:
        delta = cur_price - prev_price
        if delta < 0:
            events.append(
                make(EventKind.PRICE_DROP, f"降價 NT${-delta:,}（{prev_price:,} → {cur_price:,}）")
            )
        else:
            events.append(
                make(EventKind.PRICE_RISE, f"漲價 NT${delta:,}（{prev_price:,} → {cur_price:,}）")
            )

    # --- 目標價。只在「跨過門檻」的那一次觸發，不是每輪都報。---
    threshold = watch.price_threshold
    if threshold is not None and cur_price is not None and cur_price <= threshold:
        was_above = prev_price is None or prev_price > threshold
        if was_above:
            events.append(
                make(EventKind.BELOW_THRESHOLD, f"跌破目標價 NT${threshold:,} → NT${cur_price:,}")
            )

    return events


class Watcher:
    def __init__(
        self,
        config: Config,
        store: Store,
        client: MomoClient,
        handlers: list[Handler],
    ) -> None:
        self._config = config
        self._store = store
        self._client = client
        self._handlers = handlers

    async def _dispatch(self, event: Event) -> None:
        self._store.record_event(event)
        for handler in self._handlers:
            try:
                await handler.handle(event)
            except Exception:  # noqa: BLE001 - handler 掛掉不該中斷監控
                log.exception("handler %s 處理事件失敗", type(handler).__name__)

    async def _check(self, watch: Watch) -> list[Event]:
        try:
            fetched = await self._client.fetch(watch.code)
        except FetchError as exc:
            # 抓不到就是抓不到，保留舊狀態，下一輪再說。
            log.warning("%s", exc)
            return []

        if fetched.looks_missing:
            # 抓得到頁面但整頁沒有商品資訊。這種商品永遠不會觸發任何事件，
            # 靜靜留在清單裡只會讓人以為還在監控，所以每一輪都出聲。
            log.warning(
                "[%s] 查無商品資訊，可能編號錯誤或已下架：%s",
                watch.code,
                fetched.desktop_url,
            )

        previous = self._store.get_state(watch.code)
        current = merge_state(previous, fetched)
        events = diff(watch, previous, current)
        self._store.save_state(current)

        log.debug(
            "code=%s name=%s price=%s availability=%s events=%d",
            current.code,
            current.name,
            current.price,
            current.availability.value,
            len(events),
        )
        return events

    async def run_once(self) -> list[Event]:
        """跑一輪，回傳這輪產生的所有事件。"""
        watches = self._store.list_watches()
        if not watches:
            log.warning("監控清單是空的，先用 `momo-watch add <網址或商品編號>` 加入商品")
            return []

        results = await asyncio.gather(*(self._check(w) for w in watches))
        events = [e for batch in results for e in batch]
        for event in events:
            await self._dispatch(event)
        return events

    async def run_forever(self, stop: asyncio.Event) -> None:
        """持續輪詢直到 stop 被設起來。"""
        cfg = self._config
        log.info(
            "開始監控：間隔 %.0fs（抖動 ±%.0fs）、併發 %d",
            cfg.poll_interval,
            cfg.poll_jitter,
            cfg.concurrency,
        )
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - 迴圈必須活著
                log.exception("這一輪監控失敗，繼續下一輪")

            # 加抖動，避免每輪都在同一個整秒打點。
            delay = cfg.poll_interval + random.uniform(0, cfg.poll_jitter)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        log.info("監控結束")
