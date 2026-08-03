"""BrowserSession 與登入判別。

登入判別靠的是「人在不在 login.momo 上」—— 實測登出狀態點商品頁的購物車
按鈕就會被導到那裡，所以不必去猜 momo 的 cookie 名稱。
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")

from momo_watch.browser import (  # noqa: E402
    LOGIN_URL,
    BrowserSession,
    on_login_page,
)


class TestOnLoginPage:
    @pytest.mark.parametrize(
        "url",
        [
            "https://m.momoshop.com.tw/mymomo/login.momo",
            # 實測導向時會帶 preUrl 參數
            "https://m.momoshop.com.tw/mymomo/login.momo?preUrl=https%3A%2F%2Fwww.momoshop.com.tw%2Fproduct%2F14160587",
            "http://m.momoshop.com.tw/mymomo/login.momo?x=1",
        ],
    )
    def test_detects_login_page(self, url):
        assert on_login_page(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.momoshop.com.tw/product/14160587",
            "https://m.momoshop.com.tw/",
            "https://www.momoshop.com.tw/",
            "",
        ],
    )
    def test_other_pages_are_not_the_login_page(self, url):
        assert on_login_page(url) is False

    def test_login_url_is_itself_a_login_page(self):
        """常數與判別函式必須一致，否則 login 指令會一開場就誤判成功。"""
        assert on_login_page(LOGIN_URL)


class TestHasLoginState:
    def test_missing_file(self, tmp_path):
        session = BrowserSession(storage_state=tmp_path / "nope.json")
        assert session.has_login_state is False

    def test_existing_file(self, tmp_path):
        path = tmp_path / "storage_state.json"
        path.write_text("{}", encoding="utf-8")
        assert BrowserSession(storage_state=path).has_login_state is True

    def test_context_before_start_is_an_error(self, tmp_path):
        """還沒 start() 就拿 context 要明確報錯，不要回 None 讓錯誤延後爆。"""
        session = BrowserSession(storage_state=tmp_path / "nope.json")
        with pytest.raises(RuntimeError, match="尚未 start"):
            _ = session.context


class TestIsLoggedIn:
    """會真的連 momo。連不上就 skip —— is_logged_in 遇到例外一律回 False，
    若不特別處理，離線時這條會因為錯誤的原因「通過」，那比紅燈更糟。
    """

    async def test_reports_false_when_not_logged_in(self, browser_session):
        page = await browser_session.new_page()
        try:
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15_000)
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"連不到 momo，跳過：{str(exc).splitlines()[0][:80]}")

            # 先確認前提成立：沒登入時真的會停在登入頁。
            assert on_login_page(page.url), f"預期停在登入頁，實際在 {page.url}"
        finally:
            await page.close()

        assert await browser_session.is_logged_in(timeout=15_000) is False
