"""共用 fixture。

瀏覽器相關的測試打的是 tests/fake_shop/ 這個本地假商店，不是真的 momo：
  - CI 裡跑得動，不需要網路
  - 不會因為 momo 改版而紅
  - 更不會因為跑測試就對 momo 送出一堆請求

假商店模擬的是**流程形狀**（商品頁 → 加入購物車 → 購物車 → 付款頁），
所以驗證的是流程引擎的正確性；momo 真正的 selector 由使用者填在 flow.toml，
再用 `momo-watch buy --dry-run` 對真站驗證。
"""

from __future__ import annotations

import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FAKE_SHOP_DIR = Path(__file__).parent / "fake_shop"

# 容器裡預裝的 chromium 版本可能跟 playwright 套件對不上，用 executable_path 指過去。
_PREINSTALLED_CHROMIUM = Path("/opt/pw-browsers/chromium")
if not os.environ.get("MOMO_BROWSER_PATH") and _PREINSTALLED_CHROMIUM.exists():
    os.environ["MOMO_BROWSER_PATH"] = str(_PREINSTALLED_CHROMIUM)


@pytest.fixture(scope="session")
def fake_shop() -> str:
    """啟動假商店，回傳 base URL。

    用真的 HTTP server 而不是 file:// —— localStorage 在 file:// 下的行為
    跟真實情境不一樣，而假商店就是靠 localStorage 模擬購物車狀態。
    """
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FAKE_SHOP_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
async def browser_session():
    """乾淨的無登入態 BrowserSession。"""
    playwright = pytest.importorskip("playwright.async_api")  # noqa: F841
    from momo_watch.browser import BrowserSession

    session = BrowserSession(storage_state="/nonexistent-so-no-login-state.json")
    await session.start()
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def page(browser_session):
    p = await browser_session.new_page()
    try:
        yield p
    finally:
        await p.close()
