"""執行 Flow。

跟 flow.py 分開的理由：flow.py 只做解析與驗證，不 import playwright，
所以設定檔的測試可以在沒裝瀏覽器的環境跑。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .flow import Action, Flow, Step
from .text import pad

log = logging.getLogger(__name__)


class StepStatus(StrEnum):
    OK = "ok"
    SKIPPED_DRY_RUN = "skipped_dry_run"
    SKIPPED_OPTIONAL = "skipped_optional"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepResult:
    name: str
    action: Action
    status: StepStatus
    elapsed_ms: float
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not StepStatus.FAILED


@dataclass(slots=True)
class FlowResult:
    steps: list[StepResult] = field(default_factory=list)
    total_ms: float = 0.0
    #: 演練模式碰到第一個 mutating 步驟就停了（不是失敗）。
    stopped_for_dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def failed_step(self) -> StepResult | None:
        return next((s for s in self.steps if not s.ok), None)

    @property
    def reached(self) -> str:
        done = [s for s in self.steps if s.ok]
        return done[-1].name if done else "（尚未開始）"

    def summary(self) -> str:
        """逐步耗時。搶購場景最重要的除錯資訊就是「那幾秒花在哪」。"""
        lines = [f"{pad('步驟', 28)} {pad('狀態', 18)} {pad('耗時', 10, align='>')}"]
        for step in self.steps:
            lines.append(
                f"{pad(step.name, 28)} {pad(step.status.value, 18)} {step.elapsed_ms:7.0f} ms"
            )
        lines.append(f"{pad('總計', 28)} {pad('', 18)} {self.total_ms:7.0f} ms")
        if self.stopped_for_dry_run:
            lines.append("（演練模式：停在第一個會改變狀態的步驟前）")
        return "\n".join(lines)


class FlowRunner:
    """把 Flow 跑在一個 Playwright page 上。"""

    def __init__(self, flow: Flow, *, dry_run: bool | None = None) -> None:
        self._flow = flow
        # 明確傳入的 dry_run 優先於設定檔。
        self._dry_run = flow.guards.dry_run if dry_run is None else dry_run

    async def _verify_only(self, page: Page, step: Step) -> tuple[StepStatus, str]:
        """演練模式下只確認元素找得到，不真的動它。"""
        if step.selector:
            await page.wait_for_selector(step.selector, timeout=step.timeout_ms)
            return StepStatus.SKIPPED_DRY_RUN, f"selector {step.selector} 存在，演練未執行"
        return StepStatus.SKIPPED_DRY_RUN, "演練未執行"

    async def _execute(self, page: Page, step: Step) -> tuple[StepStatus, str]:
        match step.action:
            case Action.GOTO:
                await page.goto(step.url, timeout=step.timeout_ms, wait_until="domcontentloaded")
                return StepStatus.OK, step.url
            case Action.CLICK:
                await page.click(step.selector, timeout=step.timeout_ms)
                return StepStatus.OK, step.selector
            case Action.FILL:
                await page.fill(step.selector, step.value, timeout=step.timeout_ms)
                return StepStatus.OK, step.selector
            case Action.WAIT_FOR:
                await page.wait_for_selector(step.selector, timeout=step.timeout_ms)
                return StepStatus.OK, step.selector
            case Action.EXPECT:
                await page.wait_for_selector(step.selector, timeout=step.timeout_ms)
                return StepStatus.OK, f"找到 {step.selector}"
            case Action.EXPECT_NOT:
                await page.wait_for_selector(
                    step.selector, state="detached", timeout=step.timeout_ms
                )
                return StepStatus.OK, f"確認沒有 {step.selector}"
            case Action.SCREENSHOT:
                await page.screenshot(path=step.path, full_page=True)
                return StepStatus.OK, step.path

        raise AssertionError(f"未處理的 action: {step.action}")  # pragma: no cover

    async def run(self, page: Page, variables: dict[str, str]) -> FlowResult:
        """依序執行所有步驟。任一步驟失敗就停下（optional 的除外）。

        演練模式碰到第一個 mutating 步驟時，驗證完 selector 就**停止**而不是
        跳過後繼續。因為後面每一步都建立在那個動作真的發生過的前提上 ——
        跳過加入購物車卻繼續往購物車頁面走，只會得到一串看起來像 selector
        壞掉的假失敗，反而蓋掉真正的問題。

        所以驗證分兩階段：
          1. dry_run = true               → 驗證商品頁的 selector，零副作用
          2. dry_run = false + 停在付款前 → 驗證整條鏈，副作用只是購物車多一筆
        """
        result = FlowResult()
        started = time.perf_counter()

        for raw_step in self._flow.steps:
            step = raw_step.render(variables)
            step_started = time.perf_counter()
            dry_stop = self._dry_run and step.mutating
            try:
                if dry_stop:
                    status, detail = await self._verify_only(page, step)
                else:
                    status, detail = await self._execute(page, step)
            except (PlaywrightTimeout, PlaywrightError) as exc:
                elapsed = (time.perf_counter() - step_started) * 1000
                message = str(exc).split("\n")[0]
                if step.optional:
                    result.steps.append(
                        StepResult(
                            step.name, step.action, StepStatus.SKIPPED_OPTIONAL, elapsed, message
                        )
                    )
                    log.info("步驟「%s」跳過（optional）：%s", step.name, message)
                    continue
                result.steps.append(
                    StepResult(step.name, step.action, StepStatus.FAILED, elapsed, message)
                )
                log.error("步驟「%s」失敗：%s", step.name, message)
                break

            elapsed = (time.perf_counter() - step_started) * 1000
            result.steps.append(StepResult(step.name, step.action, status, elapsed, detail))
            log.info("步驟「%s」%s（%.0f ms）", step.name, status.value, elapsed)

            if dry_stop:
                result.stopped_for_dry_run = True
                log.info("演練模式：停在「%s」之前，後續步驟未驗證", step.name)
                break

        result.total_ms = (time.perf_counter() - started) * 1000
        return result
