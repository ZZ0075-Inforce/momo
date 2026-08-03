from pathlib import Path

import pytest

from momo_watch.models import Availability, normalize_code
from momo_watch.parser import clean_name, parse_availability, parse_price, parse_product

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestNormalizeCode:
    @pytest.mark.parametrize(
        "raw",
        [
            "6453015",
            "  6453015  ",
            # momo 目前網址列顯示的形式（舊網址都會 301 轉到這裡）
            "https://www.momoshop.com.tw/product/6453015",
            "https://www.momoshop.com.tw/product/6453015?parentId=0",
            # 舊格式仍然要吃 —— 書籤與別人分享的連結還是這個
            "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=6453015",
            "https://m.momoshop.com.tw/goods.momo?i_code=6453015",
            "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=6453015&mdiv=1099700000",
        ],
    )
    def test_extracts_code(self, raw):
        assert normalize_code(raw) == "6453015"

    @pytest.mark.parametrize("raw", ["", "not-a-url", "https://example.com/thing"])
    def test_rejects_garbage(self, raw):
        with pytest.raises(ValueError):
            normalize_code(raw)


class TestParsePrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,299", 1299),
            ("1299", 1299),
            ("1299.00", 1299),
            ("NT$13,980", 13980),
            ("13,980 元", 13980),
            (None, None),
            ("", None),
            ("洽詢", None),
        ],
    )
    def test_parse(self, raw, expected):
        assert parse_price(raw) == expected


class TestParseAvailability:
    @pytest.mark.parametrize("raw", ["in stock", "instock", "IN STOCK", " In_Stock ", "現貨"])
    def test_in_stock(self, raw):
        assert parse_availability(raw) is Availability.IN_STOCK

    @pytest.mark.parametrize("raw", ["out of stock", "oos", "sold out", "缺貨", "補貨中"])
    def test_out_of_stock(self, raw):
        assert parse_availability(raw) is Availability.OUT_OF_STOCK

    @pytest.mark.parametrize("raw", [None, "", "某個沒看過的狀態"])
    def test_unknown_not_treated_as_out_of_stock(self, raw):
        """認不出來要回 UNKNOWN，不能當成售完 —— 否則會誤報補貨。"""
        assert parse_availability(raw) is Availability.UNKNOWN


class TestParseProduct:
    def test_in_stock_page(self):
        snap = parse_product("6453015", load("in_stock.html"))
        assert snap.code == "6453015"
        assert snap.name == "【Sony】PlayStation 5 數位版主機"
        assert snap.price == 13980
        assert snap.availability is Availability.IN_STOCK
        assert snap.url == "https://m.momoshop.com.tw/goods.momo?i_code=6453015"

    def test_out_of_stock_page(self):
        snap = parse_product("6453015", load("out_of_stock.html"))
        assert snap.price == 13980
        assert snap.availability is Availability.OUT_OF_STOCK

    def test_page_without_meta_degrades_gracefully(self):
        """被擋或改版時不該爆炸，回 UNKNOWN 讓上層沿用舊狀態。"""
        snap = parse_product("6453015", load("no_meta.html"))
        assert snap.price is None
        assert snap.availability is Availability.UNKNOWN

    def test_empty_html(self):
        snap = parse_product("6453015", "")
        assert snap.availability is Availability.UNKNOWN
        assert snap.price is None


class TestCleanName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "【MAY FLOWER 五月花】新柔韌抽取式衛生紙 - momo購物網 - 好評推薦 -2026年8月",
                "【MAY FLOWER 五月花】新柔韌抽取式衛生紙",
            ),
            (
                "【Kleenex 舒潔】絲絨舒膚抽取衛生紙 - momo購物網",
                "【Kleenex 舒潔】絲絨舒膚抽取衛生紙",
            ),
            ("沒有後綴的名稱", "沒有後綴的名稱"),
            (None, None),
            ("", None),
        ],
    )
    def test_strips_seo_suffix(self, raw, expected):
        """後綴含年月，每月都會變 —— 留著會變成通知裡的雜訊。"""
        assert clean_name(raw) == expected


class TestMissingProduct:
    """momo 對不存在／已下架的商品編號回 HTTP 200 加通用殼頁，不是 404。"""

    def test_shell_page_is_flagged_as_missing(self):
        snap = parse_product("6453015", load("missing_product.html"))
        assert snap.looks_missing

    def test_shell_page_title_is_not_used_as_product_name(self):
        """殼頁的 <title> 是「momo購物網 -- Mobile管理訊息」。

        拿它當商品名稱回報比留空更糟 —— 使用者會以為抓到了商品。
        """
        snap = parse_product("6453015", load("missing_product.html"))
        assert snap.name is None

    def test_real_product_is_not_flagged_as_missing(self):
        assert not parse_product("6453015", load("in_stock.html")).looks_missing

    def test_out_of_stock_product_is_not_missing(self):
        """售完跟不存在是兩回事，不能混為一談。"""
        snap = parse_product("6453015", load("out_of_stock.html"))
        assert not snap.looks_missing
        assert snap.availability is Availability.OUT_OF_STOCK

    def test_title_fallback_still_works_on_real_product_pages(self):
        """有商品資訊但缺 og:title 時，仍然可以退回 <title>。"""
        html = """<html><head><title>某商品 - momo購物網 - 好評推薦 -2026年8月</title>
        <meta property="product:price:amount" content="1,299">
        <meta property="product:availability" content="in stock"></head></html>"""
        snap = parse_product("123", html)
        assert snap.name == "某商品"
        assert not snap.looks_missing

    def test_desktop_url_uses_current_momo_form(self):
        """momo 現在把舊網址都轉到 /product/{code}。"""
        snap = parse_product("3252331", load("in_stock.html"))
        assert snap.desktop_url == "https://www.momoshop.com.tw/product/3252331"
