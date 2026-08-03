"""Playwright session 管理。

搶購場景的關鍵是**預熱**：冷啟動一個 browser 要 2–3 秒，等偵測到補貨才開就
已經輸了。所以監控啟動時就把 browser 開好、登入態載入、目標網域先連過一次
（DNS / TLS / HTTP 連線都建立好），觸發當下只剩流程本身的耗時。

登入一律靠人工做一次：momo 有簡訊 OTP 與圖形驗證，自動化登入既不可靠也不
該做。登入後把 cookie 存成 storage_state 重複使用。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import TracebackType

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

log = logging.getLogger(__name__)

DEFAULT_STORAGE_STATE = Path("storage_state.json")
MOMO_HOME = "https://m.momoshop.com.tw/"

#: momo 的登入頁。實測：登出狀態下點商品頁的購物車按鈕就會被導到這裡，
#: 所以「人在不在這個網址上」就是最直接的登入判別訊號，不必猜 cookie 名稱。
LOGIN_URL = "https://m.momoshop.com.tw/mymomo/login.momo"

#: 判斷是否還停在登入頁用的片段。
_LOGIN_MARKER = "login.momo"


def on_login_page(url: str) -> bool:
    return _LOGIN_MARKER in url


# 手機版 UA，跟 client.py 保持一致 —— 兩邊看到的頁面才會是同一個版本。
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)


def browser_executable() -> str | None:
    """自訂 chromium 路徑。

    Playwright 平常會自己管理瀏覽器；但在已預裝 chromium 的容器裡（版本跟
    playwright 套件對不上時）要用 MOMO_BROWSER_PATH 指過去。
    """
    return os.environ.get("MOMO_BROWSER_PATH") or None


class BrowserSession:
    """一個預熱好、已登入的瀏覽器 context。"""

    def __init__(
        self,
        *,
        storage_state: Path | str = DEFAULT_STORAGE_STATE,
        headless: bool = True,
        viewport: tuple[int, int] = (414, 896),
    ) -> None:
        self._storage_state = Path(storage_state)
        self._headless = headless
        self._viewport = viewport
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def has_login_state(self) -> bool:
        return self._storage_state.is_file()

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserSession 尚未 start()")
        return self._context

    async def start(self) -> BrowserSession:
        self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, object] = {"headless": self._headless}
        if executable := browser_executable():
            launch_kwargs["executable_path"] = executable

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        context_kwargs: dict[str, object] = {
            "user_agent": MOBILE_USER_AGENT,
            "viewport": {"width": self._viewport[0], "height": self._viewport[1]},
            "locale": "zh-TW",
            "timezone_id": "Asia/Taipei",
        }
        if self.has_login_state:
            context_kwargs["storage_state"] = str(self._storage_state)
            log.info("已載入登入狀態：%s", self._storage_state)
        else:
            log.warning(
                "找不到 %s，這個 context 沒有登入。先跑 `momo-watch login`。",
                self._storage_state,
            )

        self._context = await self._browser.new_context(**context_kwargs)
        return self

    async def warm(self, url: str = MOMO_HOME) -> None:
        """先連一次目標網域，把 DNS / TLS / 連線都建立起來。

        失敗不算致命 —— 預熱只是省時間，不是流程的一部分。
        """
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            log.info("預熱完成：%s", url)
        except Exception as exc:  # noqa: BLE001 - 預熱失敗不該中斷監控
            log.warning("預熱失敗（不影響後續流程）：%s", exc)
        finally:
            await page.close()

    async def new_page(self) -> Page:
        return await self.context.new_page()

    async def save_login_state(self) -> Path:
        await self.context.storage_state(path=str(self._storage_state))
        log.info("登入狀態已存到 %s", self._storage_state)
        return self._storage_state

    async def is_logged_in(self, *, timeout: float = 20_000) -> bool:
        """檢查目前 context 的登入狀態還有沒有效。

        作法是打帶 preUrl 的登入網址：已登入的話 momo 會把你轉去 preUrl，
        還沒登入就會留在登入頁。比「storage_state.json 存不存在」可靠得多 ——
        cookie 會過期，檔案不會自己消失。
        """
        page = await self.context.new_page()
        try:
            await page.goto(
                f"{LOGIN_URL}?preUrl={MOMO_HOME}",
                wait_until="domcontentloaded",
                timeout=timeout,
            )
            await page.wait_for_timeout(1500)
            return not on_login_page(page.url)
        except Exception as exc:  # noqa: BLE001 - 檢查失敗不該讓呼叫端爆掉
            log.warning("登入狀態檢查失敗，當作未登入處理：%s", exc)
            return False
        finally:
            await page.close()

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None

    async def __aenter__(self) -> BrowserSession:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
