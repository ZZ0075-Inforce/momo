import pytest

from momo_watch.text import display_width, pad


class TestDisplayWidth:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("abc", 3),
            ("商品編號", 8),  # 全形字各佔兩欄
            ("PS5 數位版", 4 + 6),
            ("NT$13,980", 9),
        ],
    )
    def test_width(self, text, expected):
        assert display_width(text) == expected


class TestPad:
    def test_pads_ascii_left(self):
        assert pad("abc", 6) == "abc   "

    def test_pads_ascii_right(self):
        assert pad("abc", 6, align=">") == "   abc"

    def test_cjk_padding_uses_display_width(self):
        """「狀態」顯示寬度是 4，補到 8 欄應該只加 4 個空白。"""
        assert pad("狀態", 8) == "狀態    "

    def test_columns_line_up(self):
        """混合中英文的兩列，補完之後顯示寬度要一致。"""
        a = pad("商品編號", 14) + "|"
        b = pad("6453015", 14) + "|"
        assert display_width(a) == display_width(b)

    def test_overlong_text_is_not_truncated(self):
        """寬度不夠時保留原文，寧可歪掉也不要吃掉資訊。"""
        assert pad("很長的商品名稱", 4) == "很長的商品名稱"
