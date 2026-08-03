"""momo 商品頁抓取。純 HTTP，不開瀏覽器。

手機版網域（m.momoshop.com.tw）的頁面比桌面版輕很多，而我們要的 meta 標籤
兩邊都有，所以固定打手機版。
"""

from __future__ import annotations

import asyncio
import logging
import random
from types import TracebackType

import httpx

from .models import Snapshot
from .parser import parse_product

log = logging.getLogger(__name__)

BASE_URL = "https://m.momoshop.com.tw/goods.momo"

# 帶上瀏覽器該有的 header。momo 會擋掉一看就是腳本的請求（例如 python-httpx
# 的預設 UA）。這裡沒有 'authority' —— 那是 HTTP/2 的 pseudo-header，httpx
# 會自己處理，手動塞反而會重複。
DEFAULT_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "accept-language": "zh-TW,zh;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    ),
}

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """抓取失敗（重試後仍失敗）。呼叫端應視為 UNKNOWN 而非售完。"""


class MomoClient:
    """帶節流與重試的 momo 抓取器。

    節流是全域的：不論併發多少，兩次送出之間至少間隔 min_request_gap 秒。
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        concurrency: int = 4,
        min_request_gap: float = 0.5,
        max_retries: int = 2,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
            http2=False,
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._gap = min_request_gap
        self._max_retries = max_retries
        self._gate = asyncio.Lock()
        self._last_sent = 0.0

    async def __aenter__(self) -> MomoClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        """確保全域送出間隔不小於 self._gap。"""
        if self._gap <= 0:
            return
        async with self._gate:
            loop = asyncio.get_running_loop()
            wait = self._last_sent + self._gap - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_sent = loop.time()

    async def fetch(self, code: str) -> Snapshot:
        """抓一個商品。重試後仍失敗則丟 FetchError。"""
        async with self._semaphore:
            last_error: Exception | None = None
            for attempt in range(self._max_retries + 1):
                if attempt:
                    # 指數退避 + 抖動，避免多個商品同時重試打成一排。
                    backoff = 2**attempt + random.uniform(0, 1)
                    log.debug("code=%s 第 %d 次重試，等待 %.1fs", code, attempt, backoff)
                    await asyncio.sleep(backoff)

                await self._throttle()
                try:
                    resp = await self._client.get(BASE_URL, params={"i_code": code})
                except httpx.HTTPError as exc:
                    last_error = exc
                    continue

                if resp.status_code in _RETRYABLE_STATUS:
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                    continue
                if resp.status_code >= 400:
                    # 404 之類的重試也沒用，直接放棄。
                    raise FetchError(f"code={code} 回傳 HTTP {resp.status_code}")

                return parse_product(code, resp.text)

            raise FetchError(f"code={code} 重試 {self._max_retries} 次後仍失敗: {last_error}")
