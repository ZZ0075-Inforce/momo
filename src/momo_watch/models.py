"""資料型別：商品快照、監控項目、變化事件。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# momo 商品連結的三種形式，全部都吃：
#   https://www.momoshop.com.tw/product/6453015                        ← 目前網址列顯示的
#   https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=6453015   ← 舊版桌面
#   https://m.momoshop.com.tw/goods.momo?i_code=6453015                ← 舊版手機
# 舊的兩種現在都會 301 轉到 /product/{code}，但使用者手邊的書籤與別人分享的
# 連結仍然是舊格式，所以三種都得支援。
_I_CODE_RE = re.compile(r"i_code=(\d+)")
_PRODUCT_PATH_RE = re.compile(r"/product/(\d+)")
_BARE_CODE_RE = re.compile(r"^\d+$")


def normalize_code(raw: str) -> str:
    """把使用者貼上的網址或純數字統一成 i_code。

    >>> normalize_code("https://www.momoshop.com.tw/product/6453015")
    '6453015'
    >>> normalize_code("https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=6453015")
    '6453015'
    >>> normalize_code("6453015")
    '6453015'
    """
    raw = raw.strip()
    if _BARE_CODE_RE.match(raw):
        return raw
    for pattern in (_I_CODE_RE, _PRODUCT_PATH_RE):
        m = pattern.search(raw)
        if m:
            return m.group(1)
    raise ValueError(f"無法從 {raw!r} 解析出 momo 商品編號")


class Availability(StrEnum):
    """商品供應狀態。

    UNKNOWN 代表這次抓取沒能判斷（頁面改版、被擋、暫時性錯誤），
    刻意跟 OUT_OF_STOCK 分開 —— 否則一次抓取失敗會被誤判成「售完」，
    等下一輪成功時再誤報一次「補貨了」。
    """

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """單次抓取到的商品狀態。"""

    code: str
    name: str | None
    price: int | None
    availability: Availability
    fetched_at: datetime

    @property
    def url(self) -> str:
        return f"https://m.momoshop.com.tw/goods.momo?i_code={self.code}"

    @property
    def desktop_url(self) -> str:
        return f"https://www.momoshop.com.tw/product/{self.code}"

    @property
    def looks_missing(self) -> bool:
        """整頁抓不到任何商品資訊 —— 幾乎都是編號錯誤或商品已下架。

        momo 對不存在的商品編號不回 404，而是回 HTTP 200 加一個通用殼頁，
        所以光看狀態碼分不出來。三個欄位同時落空就是這個情況；把它跟
        「momo 改版導致解析失敗」分開，使用者才知道該去改編號還是修 parser。
        """
        return (
            self.name is None and self.price is None and self.availability is Availability.UNKNOWN
        )


@dataclass(frozen=True, slots=True)
class Watch:
    """一個被監控的商品。"""

    code: str
    label: str | None = None
    price_threshold: int | None = None

    def display(self, snapshot: Snapshot | None = None) -> str:
        if self.label:
            return self.label
        if snapshot and snapshot.name:
            return snapshot.name
        return self.code


class EventKind(StrEnum):
    FIRST_SEEN = "first_seen"
    RESTOCK = "restock"  # 補貨 —— 這是搶購場景真正在等的訊號
    SOLD_OUT = "sold_out"
    PRICE_DROP = "price_drop"
    PRICE_RISE = "price_rise"
    BELOW_THRESHOLD = "below_threshold"


#: 需要立刻叫醒你的事件；其餘只記錄不推播。
URGENT_EVENTS = frozenset({EventKind.RESTOCK, EventKind.BELOW_THRESHOLD})


@dataclass(frozen=True, slots=True)
class Event:
    """一次狀態變化。"""

    kind: EventKind
    watch: Watch
    snapshot: Snapshot
    previous: Snapshot | None
    detail: str

    @property
    def is_urgent(self) -> bool:
        return self.kind in URGENT_EVENTS
