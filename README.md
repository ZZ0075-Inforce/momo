# momo-watch

momo 購物網商品**庫存 / 價格監控與通知**。

> **Phase 1 範圍**：只監控、只通知，**不會自動下單也不碰付款**。
> 這是刻意的 —— 理由見下面〈為什麼先做監控〉。

---

## 為什麼先做監控

做這個專案前先查過現有方案，結論是：**momo 專用的完整自動搶購開源專案並不存在**。
最接近的三個各自只解決一半：

| 專案 | 涵蓋 | 缺什麼 |
|---|---|---|
| [chen-tf/price-tracker-bot](https://github.com/chen-tf/price-tracker-bot) | momo 降價 / 補貨通知 | 不下單 |
| [samttoo22-MewCat/Momo_WebCrawler](https://github.com/samttoo22-MewCat/Momo_WebCrawler) | momo 商品爬蟲 | 不下單 |
| [jumpingchu/PChome-AutoBuy](https://github.com/jumpingchu/PChome-AutoBuy) | 完整自動下單 | 是 PChome，不是 momo |

從 `price-tracker-bot` 挖到的關鍵事實決定了本專案的架構：**momo 把商品狀態直接
寫在 Open Graph meta 標籤裡**，所以偵測庫存**不需要開瀏覽器**：

```html
<meta property="og:title"             content="商品名稱">
<meta property="product:price:amount" content="13,980">
<meta property="product:availability" content="in stock">   <!-- 關鍵 -->
```

純 HTTP 一次約 100ms，開 Selenium/Playwright 要 2–3 秒 —— 差 30 倍。所以本專案
把問題切成兩個需求相反的半場，Phase 1 只做上面那半：

```
[監控半場] 高頻、輕量、無狀態  ← Phase 1（本專案目前的範圍）
  httpx 輪詢 meta 標籤 → 偵測 availability 翻轉
                ↓ 事件
[執行半場] 低頻、重狀態、需登入  ← Phase 2/3
  預熱的 Playwright session → 加入購物車 → 結帳
```

先做監控還有一個實際理由：**momo 頁面結構會改**。你需要一個穩定的監控層來觀察
它多久改一次、怎麼改，再決定要不要把下單邏輯壓在上面。

## 安裝

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env          # 填 Telegram token（不填也能跑，只印 console）
```

## 使用

```bash
# 先驗證解析對不對（不寫入資料庫）
momo-watch check https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=6453015

# 加入監控 —— 網址或純數字編號都吃
momo-watch add https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=6453015 \
    --label "PS5" --threshold 13000

momo-watch list          # 監控清單 + 最後狀態
momo-watch run           # 啟動監控迴圈（Ctrl-C 結束）
momo-watch run --once    # 只跑一輪，適合放進 cron
momo-watch events        # 最近的事件紀錄
momo-watch rm 6453015
```

Docker：

```bash
docker compose run --rm momo-watch add <網址> --threshold 13000
docker compose up -d
```

## 通知哪些事件

只有**緊急事件**會推到 Telegram，其餘只寫進資料庫。價格微幅波動每天推播，你會
很快把通知靜音，那等於整個專案失效。

| 事件 | 推播 | 說明 |
|---|:--:|---|
| `restock` | ✅ | 缺貨 → 有貨。**搶購場景真正在等的訊號** |
| `below_threshold` | ✅ | 跌破 `--threshold`。只在跨過門檻那次觸發 |
| `price_drop` / `price_rise` | — | 價格變動 |
| `sold_out` | — | 有貨 → 缺貨 |
| `first_seen` | — | 首次納入監控 |

## 兩個容易做錯的地方

程式碼裡有兩個看起來多餘、實際上很重要的設計：

**1. `UNKNOWN` 跟 `OUT_OF_STOCK` 是分開的狀態。**
一次逾時或 momo 換了個沒看過的字串，如果被當成「售完」，下一輪抓成功就會誤報
一次「補貨了」。半夜三點被假警報叫醒兩次，你就不會再信任這個通知了。所以解析
不出來一律回 `UNKNOWN`，而 `UNKNOWN` 參與的轉換一律不報。

**2. `merge_state()` 會沿用上一次「已知」的庫存狀態。**
否則 `缺貨 → (逾時) → 有貨` 這個序列中，逾時那次會把「缺貨」蓋掉，等真的補貨
時就比不出變化 —— 真警報被吃掉，比假警報更糟。

兩者都有測試釘住（`tests/test_watcher.py`）。

## 輪詢頻率

預設 60 秒，程式強制下限 5 秒。

沒事別調低。高頻輪詢對 momo 是負擔、對你是被封鎖風險，而且對「限時搶購」這種
**已知開賣時間**的場景幫助有限 —— 那個場景該做的是 Phase 2 的排程觸發，不是把
輪詢調到 1 秒。

## 開發

```bash
pytest              # 全部測試都是離線的，不打 momo
ruff check . && ruff format --check .
```

解析壞掉時（`check` 回報 `unknown`）的修法：把當下的頁面存成 `tests/fixtures/*.html`，
寫一個會失敗的測試重現，再改 `parser.py`。`parser.py` 是純函式、零 I/O，就是為了
讓這個流程成立。

## 路線圖

- [x] **Phase 1** — HTTP 庫存 / 價格監控 + Telegram 通知
- [ ] **Phase 2** — Playwright 預熱 session（手動登入一次存 `storage_state`），
      自動加入購物車 → 停在結帳頁 → 發通知叫人來按。搭配 `ntplib` 校時，
      因應整點開賣時本機時鐘漂移。
- [ ] **Phase 3** — 視付款方式決定能否全自動。**這裡有一道硬牆**：台灣信用卡
      線上刷卡常需 3D 驗證簡訊 OTP，無法也不該自動化。可行解是綁卡走 momo
      快速結帳（部分發卡行免 3DS，要實測自己的卡），或改用超商取貨付款繞開。

Phase 2 接進來時不用改 `watcher.py` —— 實作 `notifier.Handler` protocol 再掛進
handler 清單即可。

## 注意事項

- 自動化下單通常違反電商服務條款，最實際的後果是**帳號被停權**。
- momo 限時搶購為每天 5 檔、**每人限購 3 組**。多開帳號繞過限購不在本專案範圍內。
- 把輪詢頻率設得有節制，對你自己也是保護。
