from pathlib import Path

import pytest

from momo_watch.models import Availability, normalize_code
from momo_watch.parser import parse_availability, parse_price, parse_product

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestNormalizeCode:
    @pytest.mark.parametrize(
        "raw",
        [
            "6453015",
            "  6453015  ",
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
        assert snap.name == "momo購物網"  # 退回 <title>

    def test_empty_html(self):
        snap = parse_product("6453015", "")
        assert snap.availability is Availability.UNKNOWN
        assert snap.price is None
