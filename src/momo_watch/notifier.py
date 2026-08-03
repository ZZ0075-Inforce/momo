"""事件輸出端。

分成兩層：
  Sender  —— 送一段純文字（Phase 2 的下單結果回報也走這裡）
  Handler —— 處理 Event（Phase 2 的自動加入購物車就是一個 Handler）

Handler 做成 Protocol 是為了讓 Phase 2 的下單邏輯掛進 Watcher 而不用改
Watcher 本身。
"""

from __future__ import annotations

import html
import logging
from typing import Protocol

import httpx

from .models import Event, EventKind

log = logging.getLogger(__name__)

_ICONS = {
    EventKind.RESTOCK: "🚨",
    EventKind.BELOW_THRESHOLD: "💰",
    EventKind.PRICE_DROP: "📉",
    EventKind.PRICE_RISE: "📈",
    EventKind.SOLD_OUT: "⛔",
    EventKind.FIRST_SEEN: "👀",
}


# --- 純文字輸出 ---------------------------------------------------------


class Sender(Protocol):
    """送出一段純文字通知。"""

    async def send(self, text: str) -> None: ...


class ConsoleSender:
    async def send(self, text: str) -> None:
        log.warning("%s", text.replace("\n", " | "))


class TelegramSender:
    def __init__(self, token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._url,
                    json={
                        "chat_id": self._chat_id,
                        "text": html.escape(text),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                )
            if resp.status_code >= 400:
                log.error("Telegram 推播失敗 HTTP %s: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            # 通知掛掉不該讓監控迴圈跟著死。
            log.error("Telegram 推播錯誤: %s", exc)


class FanoutSender:
    """同時送到多個目的地；其中一個失敗不影響其他。"""

    def __init__(self, senders: list[Sender]) -> None:
        self._senders = senders

    async def send(self, text: str) -> None:
        for sender in self._senders:
            try:
                await sender.send(text)
            except Exception:  # noqa: BLE001
                log.exception("sender %s 送出失敗", type(sender).__name__)


# --- 事件處理 -----------------------------------------------------------


class Handler(Protocol):
    """事件處理器。"""

    async def handle(self, event: Event) -> None: ...


def format_event(event: Event) -> str:
    icon = _ICONS.get(event.kind, "•")
    name = event.watch.display(event.snapshot)
    price = f"NT${event.snapshot.price:,}" if event.snapshot.price is not None else "價格未知"
    return f"{icon} {name}\n{event.detail}\n目前 {price}\n{event.snapshot.desktop_url}"


class ConsoleHandler:
    """沒設定 Telegram 時的預設輸出，程式開箱即可跑。"""

    async def handle(self, event: Event) -> None:
        level = logging.WARNING if event.is_urgent else logging.INFO
        log.log(level, "%s", format_event(event).replace("\n", " | "))


class TelegramHandler:
    """把緊急事件推到 Telegram。

    只推 urgent 事件（補貨、跌破目標價）。價格微幅波動每天推播會讓你很快就
    把通知靜音，那等於整個專案失效。
    """

    def __init__(self, sender: Sender, *, urgent_only: bool = True) -> None:
        self._sender = sender
        self._urgent_only = urgent_only

    async def handle(self, event: Event) -> None:
        if self._urgent_only and not event.is_urgent:
            return
        await self._sender.send(format_event(event))


def build_sender(config) -> Sender:  # noqa: ANN001 - 避免與 config 循環匯入
    senders: list[Sender] = [ConsoleSender()]
    if config.telegram_enabled:
        senders.append(TelegramSender(config.telegram_token, config.telegram_chat_id))
    return FanoutSender(senders)


def build_handlers(config) -> list[Handler]:  # noqa: ANN001
    handlers: list[Handler] = [ConsoleHandler()]
    if config.telegram_enabled:
        handlers.append(
            TelegramHandler(TelegramSender(config.telegram_token, config.telegram_chat_id))
        )
    else:
        log.info("未設定 MOMO_TELEGRAM_TOKEN / CHAT_ID，只輸出到 console")
    return handlers
