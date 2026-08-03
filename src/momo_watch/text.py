"""終端機輸出的對齊工具。

中文字在等寬終端機佔兩欄，但 str 的 len() 算的是字元數，直接用 f-string 的
`{:<12}` 對齊中文欄位一定會歪。
"""

from __future__ import annotations

import unicodedata


def display_width(text: str) -> int:
    """字串在終端機的顯示寬度（全形字算 2）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int, *, align: str = "<") -> str:
    """依顯示寬度補空白。align 用 "<"（靠左）或 ">"（靠右）。"""
    padding = " " * max(0, width - display_width(text))
    return padding + text if align == ">" else text + padding
