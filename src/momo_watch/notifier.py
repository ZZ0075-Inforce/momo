"""事件輸出端。

刻意做成 Protocol：Phase 2 要加「自動加入購物車」時，寫一個新的 handler
掛進 Watcher 就好，不用動 Watcher 本身。
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

    def __init__(self, token: str, chat_id: str, *, urgent_only: bool = True) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._urgent_only = urgent_only

    async def handle(self, event: Event) -> None:
        if self._urgent_only and not event.is_urgent:
            return

        text = html.escape(format_event(event))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                )
            if resp.status_code >= 400:
                log.error("Telegram 推播失敗 HTTP %s: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            # 通知掛掉不該讓監控迴圈跟著死。
            log.error("Telegram 推播錯誤: %s", exc)


def build_handlers(config) -> list[Handler]:  # noqa: ANN001 - 避免與 config 循環匯入
    handlers: list[Handler] = [ConsoleHandler()]
    if config.telegram_enabled:
        handlers.append(TelegramHandler(config.telegram_token, config.telegram_chat_id))
    else:
        log.info("未設定 MOMO_TELEGRAM_TOKEN / CHAT_ID，只輸出到 console")
    return handlers
