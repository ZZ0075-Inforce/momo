"""Phase 2：偵測到補貨時自動把商品送進購物車、走到付款頁，然後**停下來**。

設計上的三個立場：

1. **永遠停在付款前。** `stop_before_payment` 預設 True，流程不包含送出付款。
   台灣信用卡線上刷卡常需 3D 驗證簡訊 OTP，本來就自動化不了；把「最後一按」
   留給人，也讓誤觸的代價從「買錯東西」降成「購物車裡多一筆」。
2. **閘門一律 fail-closed。** 價格讀不到就當作超過上限擋下來 —— 解析壞掉時
   最不該做的事就是照買。
3. **max_attempts 跨重啟有效。** 次數存在資料庫，程式重開不會重新計數。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .browser import BrowserSession
from .flow import Flow, Guards
from .models import Event
from .notifier import Sender
from .runner import FlowResult, FlowRunner
from .store import Store

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    #: 走到付款頁，等人工完成最後一步。這是正常成功的終點。
    READY_FOR_PAYMENT = "ready_for_payment"
    #: 流程中途失敗（selector 過期、商品秒殺完、逾時）。
    FAILED = "failed"
    #: 被安全閘門擋下，根本沒有開始跑。
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    allowed: bool
    reason: str


def check_guards(
    guards: Guards,
    event: Event,
    *,
    attempts: int,
    has_placeholders: bool,
) -> GuardVerdict:
    """純函式的閘門判斷，方便測試。"""
    if has_placeholders:
        return GuardVerdict(False, "flow.toml 還有 TODO 佔位符，selector 尚未填寫")

    if attempts >= guards.max_attempts:
        return GuardVerdict(
            False, f"已達嘗試上限（{attempts}/{guards.max_attempts}），不再重複下單"
        )

    price = event.snapshot.price
    if guards.max_price is not None:
        if price is None:
            # fail-closed：設了價格上限卻讀不到價格，就不該放行。
            return GuardVerdict(False, "解析不到價格，無法確認未超過上限")
        if price > guards.max_price:
            return GuardVerdict(False, f"價格 NT${price:,} 超過上限 NT${guards.max_price:,}")

    return GuardVerdict(True, "通過所有閘門")


class PurchaseHandler:
    """實作 notifier.Handler，掛進 Watcher 就會在補貨時自動跑流程。"""

    def __init__(
        self,
        flow: Flow,
        session: BrowserSession,
        store: Store,
        sender: Sender,
        *,
        dry_run: bool | None = None,
        screenshot_dir: Path | str = "screenshots",
    ) -> None:
        self._flow = flow
        self._session = session
        self._store = store
        self._sender = sender
        self._dry_run = flow.guards.dry_run if dry_run is None else dry_run
        self._screenshot_dir = Path(screenshot_dir)
        # 一次只跑一個下單流程。多商品同時補貨時，平行搶結帳只會互相打架。
        self._lock = asyncio.Lock()

    @property
    def guards(self) -> Guards:
        return self._flow.guards

    async def handle(self, event: Event) -> None:
        if not event.is_urgent:
            return

        code = event.snapshot.code
        verdict = check_guards(
            self.guards,
            event,
            attempts=self._store.count_attempts(code),
            has_placeholders=self._flow.has_placeholders,
        )
        if not verdict.allowed:
            log.warning("[%s] 下單流程未啟動：%s", code, verdict.reason)
            self._store.record_attempt(
                code, Outcome.BLOCKED, detail=verdict.reason, dry_run=self._dry_run
            )
            await self._sender.send(
                f"⏸ 未自動下單：{event.watch.display(event.snapshot)}\n{verdict.reason}"
            )
            return

        async with self._lock:
            await self._run(event)

    async def _run(self, event: Event) -> None:
        code = event.snapshot.code
        label = event.watch.display(event.snapshot)
        mode = "演練" if self._dry_run else "實際下單"
        log.warning("[%s] 啟動下單流程（%s）", code, mode)

        page = await self._session.new_page()
        try:
            runner = FlowRunner(self._flow, dry_run=self._dry_run)
            result = await runner.run(page, {"code": code})
            shot = await self._capture(page, code)
        finally:
            await page.close()

        outcome = Outcome.READY_FOR_PAYMENT if result.ok else Outcome.FAILED
        self._store.record_attempt(
            code,
            outcome,
            reached=result.reached,
            detail=(result.failed_step.detail if result.failed_step else None),
            dry_run=self._dry_run,
            total_ms=result.total_ms,
        )

        log.warning("[%s] 流程結束（%s）\n%s", code, outcome.value, result.summary())
        await self._sender.send(self._report(label, event, result, outcome, shot))

    async def _capture(self, page, code: str) -> Path | None:  # noqa: ANN001
        """不論成敗都截圖 —— 失敗時這是唯一能看出 momo 頁面長怎樣的線索。"""
        try:
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            path = self._screenshot_dir / f"{code}-{stamp}.png"
            await page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as exc:  # noqa: BLE001 - 截圖失敗不該影響結果回報
            log.warning("截圖失敗：%s", exc)
            return None

    def _report(
        self,
        label: str,
        event: Event,
        result: FlowResult,
        outcome: Outcome,
        shot: Path | None,
    ) -> str:
        price = event.snapshot.price
        lines = [
            f"{'🧪 演練完成' if self._dry_run else '🛒 下單流程完成'}：{label}",
            f"結果：{outcome.value}",
            f"價格：{f'NT${price:,}' if price is not None else '未知'}",
            f"耗時：{result.total_ms:.0f} ms",
        ]
        if result.ok:
            if self.guards.stop_before_payment and not self._dry_run:
                lines.append("⚠️ 已停在付款前，請手動完成最後一步")
        else:
            failed = result.failed_step
            lines.append(f"❌ 卡在「{failed.name}」：{failed.detail}")
            lines.append(f"最後完成的步驟：{result.reached}")
        if shot is not None:
            lines.append(f"截圖：{shot}")
        lines.append(event.snapshot.desktop_url)
        return "\n".join(lines)
