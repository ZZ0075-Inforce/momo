"""FlowRunner 的端到端測試，跑在本地假商店上。

真的開 chromium、真的點按鈕、真的換頁 —— 驗證的是流程引擎本身，
不是 momo 的 selector（那個由使用者填在 flow.toml）。
"""

from __future__ import annotations

import pytest

from momo_watch.flow import parse_flow
from momo_watch.runner import FlowRunner, StepStatus

pytest.importorskip("playwright.async_api")


def shop_flow(base: str, *, dry_run: bool = False, popup: bool = False) -> dict:
    product = f"{base}/product.html?code={{code}}" + ("&popup=1" if popup else "")
    return {
        "guards": {"dry_run": dry_run},
        "steps": [
            {"name": "開啟商品頁", "action": "goto", "url": product},
            {
                "name": "關閉彈窗",
                "action": "click",
                "selector": "#popup-close",
                "optional": True,
                "timeout_ms": 800,
            },
            {"name": "確認有加入購物車鈕", "action": "expect", "selector": "#add-cart"},
            {"name": "加入購物車", "action": "click", "selector": "#add-cart", "mutating": True},
            {"name": "等待加入成功", "action": "wait_for", "selector": "#cart-added"},
            {"name": "前往購物車", "action": "goto", "url": f"{base}/cart.html"},
            {"name": "確認商品在購物車", "action": "expect", "selector": "#cart-item"},
            {"name": "前往結帳", "action": "click", "selector": "#checkout", "mutating": True},
            {"name": "確認已到付款頁", "action": "expect", "selector": "#payment-form"},
        ],
    }


async def run(flow_dict, page, code="6453015"):
    flow = parse_flow(flow_dict)
    return await FlowRunner(flow).run(page, {"code": code})


class TestHappyPath:
    async def test_full_flow_reaches_payment_page(self, fake_shop, page):
        result = await run(shop_flow(fake_shop), page)

        assert result.ok, f"流程失敗於 {result.failed_step}"
        assert result.reached == "確認已到付款頁"
        assert not result.stopped_for_dry_run
        assert await page.is_visible("#payment-form")

    async def test_payment_is_never_submitted(self, fake_shop, page):
        """整條流程跑完，付款按鈕仍然沒有被按過 —— 這是最重要的一條斷言。"""
        await run(shop_flow(fake_shop), page)

        assert await page.is_visible("#payment-form")
        assert await page.is_hidden("#payment-submitted")

    async def test_cart_actually_received_the_item(self, fake_shop, page):
        await run(shop_flow(fake_shop), page)
        assert await page.evaluate("localStorage.getItem('cart')") == "6453015"

    async def test_每步都有計時(self, fake_shop, page):
        result = await run(shop_flow(fake_shop), page)
        assert all(s.elapsed_ms >= 0 for s in result.steps)
        assert result.total_ms >= sum(s.elapsed_ms for s in result.steps) * 0.9
        assert "總計" in result.summary()


class TestDryRun:
    async def test_stops_at_first_mutating_step(self, fake_shop, page):
        result = await run(shop_flow(fake_shop, dry_run=True), page)

        assert result.ok
        assert result.stopped_for_dry_run
        assert result.steps[-1].name == "加入購物車"
        assert result.steps[-1].status is StepStatus.SKIPPED_DRY_RUN

    async def test_has_no_side_effects(self, fake_shop, page):
        """演練不能真的把東西放進購物車。"""
        await run(shop_flow(fake_shop, dry_run=True), page)
        assert await page.evaluate("localStorage.getItem('cart')") is None

    async def test_still_validates_the_selector(self, fake_shop, page):
        """演練的價值就在這裡：確認 selector 找得到。"""
        result = await run(shop_flow(fake_shop, dry_run=True), page)
        assert "#add-cart" in result.steps[-1].detail

    async def test_bad_selector_fails_even_in_dry_run(self, fake_shop, page):
        flow = shop_flow(fake_shop, dry_run=True)
        flow["steps"][3]["selector"] = "#does-not-exist"
        flow["steps"][3]["timeout_ms"] = 800

        result = await run(flow, page)

        assert not result.ok
        assert result.failed_step.name == "加入購物車"


class TestOptionalSteps:
    async def test_skipped_when_element_absent(self, fake_shop, page):
        result = await run(shop_flow(fake_shop, popup=False), page)

        popup_step = next(s for s in result.steps if s.name == "關閉彈窗")
        assert popup_step.status is StepStatus.SKIPPED_OPTIONAL
        assert result.ok, "optional 步驟找不到元素不該讓整個流程失敗"

    async def test_executed_when_element_present(self, fake_shop, page):
        result = await run(shop_flow(fake_shop, popup=True), page)

        popup_step = next(s for s in result.steps if s.name == "關閉彈窗")
        assert popup_step.status is StepStatus.OK
        assert result.ok


class TestFailures:
    async def test_flow_stops_at_first_failure(self, fake_shop, page):
        flow = shop_flow(fake_shop)
        flow["steps"][2]["selector"] = "#missing"
        flow["steps"][2]["timeout_ms"] = 800

        result = await run(flow, page)

        assert not result.ok
        assert result.failed_step.name == "確認有加入購物車鈕"
        # 失敗之後的步驟完全不該執行
        assert len(result.steps) == 3
        assert result.reached == "關閉彈窗"

    async def test_failure_detail_is_a_single_line(self, fake_shop, page):
        """Playwright 的錯誤訊息很長，回報時只留第一行才讀得下去。"""
        flow = shop_flow(fake_shop)
        flow["steps"][2]["selector"] = "#missing"
        flow["steps"][2]["timeout_ms"] = 500

        result = await run(flow, page)

        assert "\n" not in result.failed_step.detail
        assert result.failed_step.detail

    async def test_reached_when_nothing_succeeded(self, fake_shop, page):
        flow = {
            "steps": [
                {
                    "name": "壞掉的第一步",
                    "action": "goto",
                    "url": "http://127.0.0.1:1/",
                    "timeout_ms": 800,
                }
            ]
        }
        result = await run(flow, page)
        assert not result.ok
        assert result.reached == "（尚未開始）"


class TestActions:
    async def test_fill_and_expect_not(self, fake_shop, page):
        flow = {
            "steps": [
                {"name": "去付款頁", "action": "goto", "url": f"{fake_shop}/checkout.html"},
                {"name": "填末三碼", "action": "fill", "selector": "#cvv", "value": "123"},
                {
                    "name": "確認尚未送出付款",
                    "action": "expect_not",
                    "selector": "#nonexistent-marker",
                    "timeout_ms": 800,
                },
            ]
        }
        result = await run(flow, page)

        assert result.ok
        assert await page.input_value("#cvv") == "123"

    async def test_screenshot_writes_a_file(self, fake_shop, page, tmp_path):
        target = tmp_path / "shot-{code}.png"
        flow = {
            "steps": [
                {"name": "去商品頁", "action": "goto", "url": f"{fake_shop}/product.html"},
                {"name": "截圖", "action": "screenshot", "path": str(target)},
            ]
        }
        result = await run(flow, page, code="999")

        assert result.ok
        written = tmp_path / "shot-999.png"
        assert written.is_file()
        assert written.stat().st_size > 0
