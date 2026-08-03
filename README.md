# momo-watch

momo 購物網商品**庫存監控**與**半自動下單**。

> **下單流程永遠停在付款前，不會自動送出付款。** 這是刻意的設計，不是尚未完成的功能 ——
> 理由見〈為什麼停在付款前〉。

---

## 為什麼做這個

做之前先查過現有方案：**momo 專用的完整自動搶購開源專案並不存在**。最接近的三個各自只解決一半：

| 專案 | 涵蓋 | 缺什麼 |
|---|---|---|
| [chen-tf/price-tracker-bot](https://github.com/chen-tf/price-tracker-bot) | momo 降價 / 補貨通知 | 不下單 |
| [samttoo22-MewCat/Momo_WebCrawler](https://github.com/samttoo22-MewCat/Momo_WebCrawler) | momo 商品爬蟲 | 不下單 |
| [jumpingchu/PChome-AutoBuy](https://github.com/jumpingchu/PChome-AutoBuy) | 完整自動下單 | 是 PChome，不是 momo |

從 `price-tracker-bot` 挖到的關鍵事實決定了整體架構：**momo 把商品狀態寫在 Open Graph
meta 標籤裡**，偵測庫存**不需要開瀏覽器**：

```html
<meta property="og:title"             content="商品名稱">
<meta property="product:price:amount" content="13,980">
<meta property="product:availability" content="in stock">   <!-- 關鍵 -->
```

純 HTTP 一次約 100ms，開瀏覽器要 2–3 秒 —— 差 30 倍。所以本專案把問題切成兩個需求
相反的半場：

```
[監控半場] 高頻、輕量、無狀態
  httpx 輪詢 meta 標籤 → 偵測 availability 翻轉
                ↓ Event
[執行半場] 低頻、重狀態、需登入
  預熱好的 Playwright session → 加入購物車 → 結帳 → 停在付款前
```

## 安裝

```bash
uv venv
uv pip install -e ".[dev]"      # 只要監控的話：uv pip install -e .
cp .env.example .env            # 填 Telegram token（不填也能跑，只印 console）
```

下單流程需要瀏覽器：

```bash
uv pip install -e ".[buy]"
playwright install chromium
```

## 監控

```bash
# 先驗證解析對不對（不寫入資料庫）
momo-watch check https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=6453015

# 加入監控 —— 網址或純數字編號都吃
momo-watch add <網址> --label "PS5" --threshold 13000

momo-watch list          # 監控清單 + 最後狀態
momo-watch run           # 啟動監控迴圈（Ctrl-C 結束）
momo-watch run --once    # 只跑一輪，適合放進 cron
momo-watch events        # 最近的事件紀錄
```

### 通知哪些事件

只有**緊急事件**會推到 Telegram，其餘只寫進資料庫。價格微幅波動每天推播，你會很快把
通知靜音，那等於整個專案失效。

| 事件 | 推播 | 說明 |
|---|:--:|---|
| `restock` | ✅ | 缺貨 → 有貨。**搶購場景真正在等的訊號** |
| `below_threshold` | ✅ | 跌破 `--threshold`。只在跨過門檻那次觸發 |
| `price_drop` / `price_rise` | — | 價格變動 |
| `sold_out` | — | 有貨 → 缺貨 |
| `first_seen` | — | 首次納入監控 |

## 下單

### 1. 登入一次

```bash
momo-watch login       # 開有畫面的瀏覽器，你手動登入，Enter 存下 cookie
```

刻意不自動化登入：momo 有簡訊 OTP 與圖形驗證，自動化既不可靠也不該做。登入狀態存在
`storage_state.json`，**等同帳號憑證，已在 .gitignore 排除**。

### 2. 填 selector

```bash
cp flow.example.toml flow.toml
```

商品頁的 selector 已經實測填好（2026-08 對真實 momo 驗證過）。**購物車與結帳頁的
還是 `TODO_`，必須你自己填** —— 那些步驟要先有東西在購物車裡才看得到。

抓法：

```bash
python tools/probe_selectors.py 3252331 8037995     # 商品頁，免登入
python tools/probe_selectors.py --url "https://www.momoshop.com.tw/cart/..."   # 需先 login
```

**momo 沒有穩定的 `id` 或 `data-*` 屬性**，一般「優先用 id」的建議在這裡行不通。
實測抓到的按鈕長這樣：

```html
<button class="h-[40px] flex-1 rounded bg-[#1169BB] ...">加入購物車</button>
```

全是 Tailwind 工具類別，編碼了顏色（`bg-[#1169BB]`）和尺寸（`h-[40px]`），改個設計
就失效。所以這裡用**文字選擇器**：

```
button:text-is("加入購物車") >> visible=true
```

`>> visible=true` 不能省：頁面上同時有兩個「加入購物車」，一個是主按鈕，一個藏在
規格選擇的底部彈層裡。不過濾的話 Playwright 會選到隱藏的那個。

有規格（顏色/尺寸）的商品，點主按鈕會先跳規格選擇彈層，要在彈層裡再確認一次。
範本裡有這兩步，設成 `optional = true`，無規格商品會自動跳過。

### 3. 分兩階段驗證

```bash
momo-watch buy 6453015 --dry-run   # 階段一：零副作用
momo-watch buy 6453015 --live      # 階段二：購物車多一筆，可自行刪除
```

| | 驗證到哪 | 副作用 |
|---|---|---|
| `--dry-run` | 商品頁的 selector | 無 |
| `--live` | 整條鏈到付款頁 | 購物車多一筆（可還原） |
| 付款 | — | **永遠人工** |

演練模式碰到第一個 `mutating` 步驟就**停下來**，不是跳過後繼續。因為後面每一步都建立
在那個動作真的發生過的前提上 —— 跳過加入購物車卻繼續往購物車頁走，只會得到一串看起來
像 selector 壞掉的假失敗，反而蓋掉真正的問題。

也因為演練只跑到那裡，**`--dry-run` 只檢查那段的 selector 有沒有填**。所以你可以先填
商品頁、驗證通過，再回頭填購物車與結帳 —— 不必一次湊齊（那些步驟本來就要先把東西放進
購物車才看得到，硬要求一次填完等於逼人瞎猜）。

### 4. 掛上監控

```bash
momo-watch run --buy       # 補貨時自動跑流程
momo-watch attempts        # 看下單嘗試紀錄
```

啟動時就把瀏覽器開好、登入態載入、momo 先連過一次。冷啟動一個 browser 要 2–3 秒，
等偵測到補貨才開就已經輸了。

### 限時搶購（已知開賣時間）

```bash
momo-watch arm 6453015 --at "2026-08-11T00:00:00+08:00" --lead 0.5
```

會先跟 `time.stdtime.gov.tw` 校時再倒數。容器 / VPS 的時鐘漂移個幾百毫秒是常態，
整點開賣時這會直接決定成敗。

## 安全閘門

`flow.toml` 的 `[guards]`，預設值全部偏保守：

| 閘門 | 預設 | 作用 |
|---|---|---|
| `stop_before_payment` | `true` | 流程不含送出付款 |
| `dry_run` | `true` | 只驗證 selector，不真的操作 |
| `max_attempts` | `1` | 同商品只試一次，**跨重啟有效**（次數存資料庫） |
| `max_price` | 未設 | 超過就不下手 |

**閘門一律 fail-closed**：設了 `max_price` 卻讀不到價格時，會擋下而不是放行。解析壞掉時
最不該做的事就是照買。

### 為什麼停在付款前

台灣信用卡線上刷卡常需 3D 驗證簡訊 OTP，本來就自動化不了。把「最後一按」留給人，
也讓誤觸的代價從「買錯東西」降成「購物車裡多一筆」。

要繞開 3DS 的話，實務上是綁卡走 momo 快速結帳（部分發卡行免 3DS，得實測自己的卡），
或改用超商取貨付款。但即使如此，本專案仍不會替你送出付款。

## 三個容易做錯的地方

程式碼裡有三個看起來多餘、實際上很重要的設計，都有測試釘住：

**1. `UNKNOWN` 跟 `OUT_OF_STOCK` 是分開的狀態。**
一次逾時如果被當成「售完」，下一輪抓成功就會誤報一次「補貨了」。半夜被假警報叫醒兩次，
你就再也不會信任這個通知——那整個專案就白做了。所以解析不出來一律回 `UNKNOWN`，
而 `UNKNOWN` 參與的轉換一律不報。

**2. `merge_state()` 沿用上一次「已知」的庫存狀態。**
否則 `缺貨 →(逾時)→ 有貨` 這個序列中，逾時那次會把「缺貨」蓋掉，等真的補貨時就比不出
變化。**真警報被吃掉，比假警報更糟。**

**3. 演練模式停在第一個 mutating 步驟，而不是跳過它。**
理由見上面〈分兩階段驗證〉。

## 輪詢頻率

預設 60 秒，程式強制下限 5 秒。

沒事別調低。高頻輪詢對 momo 是負擔、對你是被封鎖風險，而且對「已知開賣時間」的場景
幫助有限 —— 那個場景該用 `momo-watch arm`，不是把輪詢調到 1 秒。

## 開發

```bash
pytest                  # 全部離線，不會打到 momo
ruff check . && ruff format --check .
```

測試分三層：

- **純函式**（parser / flow / clock / watcher 的 diff）—— 不需要瀏覽器
- **整合**（watcher + store + handler）—— 用 stub client
- **端到端**（runner / purchase）—— 真的開 chromium，打 `tests/fake_shop/` 這個本地假商店

假商店模擬的是**流程形狀**（商品頁 → 加入購物車 → 購物車 → 付款頁），驗證的是流程引擎
本身。momo 真正的 selector 由你填在 `flow.toml`，再用 `momo-watch buy --dry-run` 對真站
驗證 —— 這是唯一無法在 CI 裡覆蓋的部分。

其中一條端到端測試專門斷言：整條流程跑完，付款按鈕仍然沒被按過。

解析壞掉時（`check` 回報 `unknown`）的修法：把當下的頁面存成 `tests/fixtures/*.html`，
寫一個會失敗的測試重現，再改 `parser.py`。`parser.py` 是純函式、零 I/O，就是為了讓這個
流程成立。

### 容器內的 chromium

若容器已預裝 chromium 而版本跟 playwright 套件對不上：

```bash
export MOMO_BROWSER_PATH=/opt/pw-browsers/chromium
```

## 注意事項

- 自動化下單通常違反電商服務條款，最實際的後果是**帳號被停權**。
- momo 限時搶購為每天 5 檔、**每人限購 3 組**。多開帳號繞過限購不在本專案範圍內。
- `storage_state.json` 等同你的登入憑證：別提交、別放共用目錄、別複製到別台機器。
- 把輪詢頻率設得有節制，對你自己也是保護。
