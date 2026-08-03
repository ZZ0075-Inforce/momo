"""從 momo 商品頁的 HTML 解析出名稱 / 價格 / 庫存。

核心發現（來自 chen-tf/price-tracker-bot 的作法）：momo 把商品狀態直接寫在
Open Graph meta 標籤裡，所以**不需要開瀏覽器**，一個普通 HTTP GET 就拿得到。

    <meta property="og:title"             content="商品名稱">
    <meta property="product:price:amount" content="1,299">
    <meta property="product:availability" content="in stock">

這個模組是純函式、零 I/O，所以可以完全離線測試 —— momo 一改版，
先在這裡加 fixture 重現，再改解析邏輯。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from .models import Availability, Snapshot

_IN_STOCK_TOKENS = {"instock", "in stock", "in_stock", "available", "有貨", "現貨"}
_OUT_OF_STOCK_TOKENS = {
    "outofstock",
    "out of stock",
    "out_of_stock",
    "oos",
    "soldout",
    "sold out",
    "sold_out",
    "discontinued",
    "缺貨",
    "售完",
    "補貨中",
}

_PRICE_CLEAN_RE = re.compile(r"[^\d.]")

#: momo 在 <title> 與 og:title 後面接的 SEO 後綴，例如
#: 「 - momo購物網 - 好評推薦 -2026年8月」。
_SEO_SUFFIX_RE = re.compile(r"\s*-\s*momo購物網.*$", re.S)


def _meta(tree: HTMLParser, prop: str) -> str | None:
    """讀 <meta> 的 content，property= 與 name= 兩種寫法都試。"""
    for attr in ("property", "name"):
        node = tree.css_first(f'meta[{attr}="{prop}"]')
        if node is not None:
            content = node.attributes.get("content")
            if content and content.strip():
                return content.strip()
    return None


def parse_price(raw: str | None) -> int | None:
    """'1,299' / '1299.00' / 'NT$1,299' -> 1299。"""
    if not raw:
        return None
    cleaned = _PRICE_CLEAN_RE.sub("", raw)
    if not cleaned:
        return None
    try:
        # momo 標的是整數台幣，但 OG 規格允許小數，先走 float 再取整。
        return int(float(cleaned))
    except ValueError:
        return None


def parse_availability(raw: str | None) -> Availability:
    """把 product:availability 的字串對應到列舉。

    認不出來的字串一律回 UNKNOWN 而非 OUT_OF_STOCK —— 寧可漏報也不要因為
    momo 換了一個沒看過的詞就誤判成售完，接著在下一輪誤報「補貨」。
    """
    if not raw:
        return Availability.UNKNOWN
    token = raw.strip().lower().replace("-", " ")
    if token in _IN_STOCK_TOKENS or token.replace(" ", "") in _IN_STOCK_TOKENS:
        return Availability.IN_STOCK
    if token in _OUT_OF_STOCK_TOKENS or token.replace(" ", "") in _OUT_OF_STOCK_TOKENS:
        return Availability.OUT_OF_STOCK
    return Availability.UNKNOWN


def clean_name(raw: str | None) -> str | None:
    """去掉 momo 加在商品名後面的 SEO 後綴。

    實際抓到的是：
        「【MAY FLOWER 五月花】新柔韌抽取式衛生紙 - momo購物網 - 好評推薦 -2026年8月」
    後綴含年月，每個月都會變 —— 留著不只是通知裡的雜訊，日後若要比對名稱變化
    也會每月誤判一次。
    """
    if not raw:
        return None
    return _SEO_SUFFIX_RE.sub("", raw).strip() or None


def parse_product(code: str, html: str, *, fetched_at: datetime | None = None) -> Snapshot:
    """把商品頁 HTML 解析成 Snapshot。解析不到的欄位留 None / UNKNOWN。

    注意 momo 對不存在／已下架的商品編號是回 **HTTP 200** 加一個通用殼頁，
    不是 404。那種頁面沒有任何 og 標籤，三個欄位都會是空的 ——
    見 Snapshot.looks_missing。
    """
    tree = HTMLParser(html)
    name = clean_name(_meta(tree, "og:title"))
    price = parse_price(_meta(tree, "product:price:amount"))
    availability = parse_availability(_meta(tree, "product:availability"))

    # 只有在確實是商品頁時才退回 <title>。否則會把 momo 的殼頁標題
    # （「momo購物網 -- Mobile管理訊息」）當成商品名稱回報出去，比留空更糟。
    if name is None and (price is not None or availability is not Availability.UNKNOWN):
        title = tree.css_first("title")
        if title is not None:
            name = clean_name(title.text())

    return Snapshot(
        code=code,
        name=name,
        price=price,
        availability=availability,
        fetched_at=fetched_at or datetime.now(UTC),
    )
