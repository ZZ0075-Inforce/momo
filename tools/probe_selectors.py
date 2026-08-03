#!/usr/bin/env python
"""列出 momo 頁面上的候選 selector，並驗證它們是否唯一。

momo 的前端沒有穩定的 id 或 data-* 屬性，只有 Tailwind 工具類別
（`bg-[#1169BB]`、`h-[40px]` 這種），改個設計就失效。所以每次 momo 改版，
flow.toml 的 selector 都得重新推導一次 —— 這支腳本就是做那件事的。

用法：
    python tools/probe_selectors.py                      # 用預設商品探測
    python tools/probe_selectors.py 3252331 8037995      # 跨多個商品驗證唯一性
    python tools/probe_selectors.py --url https://...    # 探測任意頁面（購物車、結帳）

購物車與結帳頁需要登入。先跑 `momo-watch login`，這支腳本會自動沿用
storage_state.json。

需要 pip install -e ".[buy]"。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

# 想在頁面上找的按鈕文字。改版後若找不到，先加關鍵字再說。
KEYWORDS = (
    "加入購物車",
    "直接購買",
    "立即購買",
    "我要購買",
    "結帳",
    "去結帳",
    "前往結帳",
    "確認訂單",
    "選擇規格",
)

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)

#: 收集候選元素，並在每個節點打上 data-momo-probe 序號。
#: 可見性不在這裡判斷 —— 手寫的 rect > 0 比 Playwright 的判定寬鬆
#: （它還會看 visibility、opacity、祖先的 display），兩邊不一致會誤導。
#: 所以這裡只負責標記，可見與否交給 Playwright 自己回答。
_COLLECT_JS = """(keywords) => {
    const out = [];
    const nodes = document.querySelectorAll(
        'a, button, input[type=button], input[type=submit], [role=button], [onclick]'
    );
    let i = 0;
    for (const el of nodes) {
        const text = (el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
        if (!text || text.length > 20) continue;
        if (!keywords.some(k => text.includes(k))) continue;
        const attrs = {};
        for (const a of el.attributes) {
            if (['id', 'name', 'href'].includes(a.name) || a.name.startsWith('data-')) {
                attrs[a.name] = a.value.slice(0, 80);
            }
        }
        const probeId = String(i++);
        el.setAttribute('data-momo-probe', probeId);
        out.push({
            probeId,
            tag: el.tagName.toLowerCase(),
            text,
            attrs,
            cls: (el.className && typeof el.className === 'string')
                ? el.className.slice(0, 100) : '',
        });
    }
    return out;
}"""


def suggest(row: dict) -> str:
    """給一個候選元素，建議最穩的 selector。

    優先序刻意跟一般建議相反：momo 沒有 id / data-*，文字才是最穩的。
    data-momo-probe 是這支腳本自己打上去的，不能當 selector 用。
    """
    if el_id := row["attrs"].get("id"):
        return f"#{el_id}"
    for key, value in row["attrs"].items():
        if key.startswith("data-") and key != "data-momo-probe":
            return f'[{key}="{value}"]'
    return f'{row["tag"]}:text-is("{row["text"]}") >> visible=true'


async def probe(page, url: str) -> list[dict]:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)  # 等前端把按鈕畫出來
    print(f"\n{'=' * 72}\nURL   : {page.url}\nTITLE : {await page.title()}")

    rows = await page.evaluate(_COLLECT_JS, list(KEYWORDS))
    if not rows:
        print("找不到任何候選元素。可能是：沒登入、商品已下架，或關鍵字要更新。")
        return []

    # 可見性由 Playwright 判定，跟流程引擎實際執行時的認知一致。
    for row in rows:
        row["visible"] = await page.locator(f'[data-momo-probe="{row["probeId"]}"]').is_visible()

    print(f"\n候選元素 {len(rows)} 個：")
    for row in rows:
        mark = "可見" if row["visible"] else "隱藏"
        print(f"\n  [{mark}] <{row['tag']}> {row['text']!r}")
        for key, value in row["attrs"].items():
            print(f"         {key} = {value!r}")
        if row["cls"]:
            print(f"         class = {row['cls']!r}")
        print(f"    建議 → {suggest(row)}")

    hidden = sum(1 for r in rows if not r["visible"])
    if hidden:
        print(
            f"\n注意：有 {hidden} 個同文字的隱藏元素（多半是規格選擇彈層裡的），"
            "\n      所以 selector 一定要帶 `>> visible=true`，否則會選到錯的那個。"
        )
    return rows


async def verify(page, url: str, selectors: list[str]) -> dict[str, int]:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)
    counts = {}
    for sel in selectors:
        try:
            counts[sel] = await page.locator(sel).count()
        except Exception:
            counts[sel] = -1
    return counts


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("codes", nargs="*", default=None, help="momo 商品編號，可給多個以驗證唯一性")
    ap.add_argument("--url", help="直接探測任意網址（購物車、結帳頁）")
    ap.add_argument("--headed", action="store_true", help="開有畫面的瀏覽器")
    args = ap.parse_args()

    codes = args.codes or ["3252331", "8037995", "15220069"]
    urls = [args.url] if args.url else [f"https://www.momoshop.com.tw/product/{c}" for c in codes]

    storage = Path(os.environ.get("MOMO_STORAGE_STATE", "storage_state.json"))
    async with async_playwright() as p:
        launch: dict = {"headless": not args.headed}
        if exe := os.environ.get("MOMO_BROWSER_PATH"):
            launch["executable_path"] = exe
        browser = await p.chromium.launch(**launch)

        ctx_kwargs: dict = {
            "user_agent": MOBILE_UA,
            "locale": "zh-TW",
            "viewport": {"width": 414, "height": 896},
        }
        if storage.is_file():
            ctx_kwargs["storage_state"] = str(storage)
            print(f"（已載入登入狀態 {storage}）")
        else:
            print(f"（沒有 {storage}，未登入 —— 購物車與結帳頁會探不到東西）")

        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        first = await probe(page, urls[0])

        # 有多個網址時，拿第一頁的建議 selector 去其他頁驗證唯一性。
        if len(urls) > 1 and first:
            selectors = sorted({suggest(r) for r in first if r["visible"]})
            print(f"\n{'=' * 72}\n跨 {len(urls)} 個頁面驗證唯一性（要的是每頁都剛好 1 個）\n")
            header = f"{'selector':<52}" + "".join(f"{u.rsplit('/', 1)[-1]:>12}" for u in urls)
            print(header)
            print("-" * len(header))
            table = {sel: {} for sel in selectors}
            for url in urls:
                for sel, n in (await verify(page, url, selectors)).items():
                    table[sel][url] = n
            for sel in selectors:
                cells = "".join(
                    f"{f'{table[sel][u]}/' + ('OK' if table[sel][u] == 1 else 'NG'):>12}"
                    for u in urls
                )
                print(f"{sel:<52}{cells}")

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
