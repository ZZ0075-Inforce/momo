"""宣告式流程引擎：把「加入購物車 → 結帳」寫成設定檔而非 Python。

**為什麼是設定檔而不是寫死的 selector**

momo 的購物車 / 結帳頁面 DOM 沒有公開文件，而且會改版。把 selector 寫死在
Python 裡，每次改版都要改程式、重跑測試、重新部署。放進 `flow.toml` 之後，
改版時你只要開 DevTools 抄一次新的 selector，存檔就好。

也因為引擎本身跟 momo 完全無關，它可以用一個本地假商店做端到端測試
（見 tests/fake_shop/），不必真的去打 momo。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 5_000


class Action(StrEnum):
    GOTO = "goto"
    CLICK = "click"
    FILL = "fill"
    WAIT_FOR = "wait_for"
    EXPECT = "expect"
    EXPECT_NOT = "expect_not"
    SCREENSHOT = "screenshot"


class FlowError(RuntimeError):
    """流程設定有問題，或執行到一半失敗。"""


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    action: Action
    selector: str | None = None
    url: str | None = None
    value: str | None = None
    path: str | None = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    #: 會改變伺服器狀態的動作（加入購物車、送出結帳）。dry-run 時只驗證
    #: selector 存在但不真的點下去。
    mutating: bool = False
    #: 找不到元素時跳過而不是讓整個流程失敗（例如偶爾才出現的優惠彈窗）。
    optional: bool = False

    def render(self, variables: dict[str, str]) -> Step:
        """把 {code} 這類佔位符換成實際值。"""

        def sub(text: str | None) -> str | None:
            if text is None:
                return None
            try:
                return text.format(**variables)
            except KeyError as exc:
                raise FlowError(f"步驟「{self.name}」用到未知的變數 {exc}") from exc

        return Step(
            name=self.name,
            action=self.action,
            selector=sub(self.selector),
            url=sub(self.url),
            value=sub(self.value),
            path=sub(self.path),
            timeout_ms=self.timeout_ms,
            mutating=self.mutating,
            optional=self.optional,
        )


@dataclass(frozen=True, slots=True)
class Guards:
    """安全閘門。預設值全部偏保守，要放寬得自己明確改設定。"""

    #: 超過這個價格就不下手。解析壞掉導致價格讀錯時，這是最後一道防線。
    max_price: int | None = None
    #: 流程結束於付款頁面，永遠不自動送出付款。
    stop_before_payment: bool = True
    #: 只演練不真的按下 mutating 的步驟。
    dry_run: bool = True
    #: 同一個商品最多嘗試幾次（補貨事件可能連續觸發）。
    max_attempts: int = 1


def placeholder_steps(steps: list[Step]) -> list[str]:
    """回傳還留著 TODO 佔位符的步驟名稱。"""
    return [s.name for s in steps if "TODO" in (s.selector or "") or "TODO" in (s.url or "")]


@dataclass(frozen=True, slots=True)
class Flow:
    steps: list[Step]
    guards: Guards = field(default_factory=Guards)

    @property
    def has_placeholders(self) -> bool:
        """設定檔還留著 TODO 佔位符 —— 代表使用者還沒填完真正的 selector。"""
        return bool(placeholder_steps(self.steps))

    @property
    def dry_run_steps(self) -> list[Step]:
        """演練模式實際會碰到的步驟：到第一個 mutating 為止（含）。

        用途是讓 selector 可以分批填：先填好商品頁的部分就能 dry-run 驗證，
        不必等購物車與結帳頁的 selector 也湊齊。那些後面的步驟本來就要先
        把東西放進購物車才看得到，硬要求一次填完等於逼人瞎猜。
        """
        out: list[Step] = []
        for step in self.steps:
            out.append(step)
            if step.mutating:
                break
        return out


def _parse_step(raw: dict[str, Any], index: int) -> Step:
    if "name" not in raw:
        raise FlowError(f"第 {index + 1} 個步驟缺少 name")
    name = raw["name"]

    try:
        action = Action(raw["action"])
    except KeyError:
        raise FlowError(f"步驟「{name}」缺少 action") from None
    except ValueError:
        valid = ", ".join(a.value for a in Action)
        raise FlowError(f"步驟「{name}」的 action={raw['action']!r} 無效，可用：{valid}") from None

    step = Step(
        name=name,
        action=action,
        selector=raw.get("selector"),
        url=raw.get("url"),
        value=raw.get("value"),
        path=raw.get("path"),
        timeout_ms=int(raw.get("timeout_ms", DEFAULT_TIMEOUT_MS)),
        mutating=bool(raw.get("mutating", False)),
        optional=bool(raw.get("optional", False)),
    )

    required: dict[Action, str] = {
        Action.GOTO: "url",
        Action.CLICK: "selector",
        Action.FILL: "selector",
        Action.WAIT_FOR: "selector",
        Action.EXPECT: "selector",
        Action.EXPECT_NOT: "selector",
        Action.SCREENSHOT: "path",
    }
    field_name = required[action]
    if getattr(step, field_name) is None:
        raise FlowError(f"步驟「{name}」的 action={action.value} 需要 {field_name}")
    if action is Action.FILL and step.value is None:
        raise FlowError(f"步驟「{name}」的 action=fill 需要 value")

    return step


def parse_flow(data: dict[str, Any]) -> Flow:
    raw_steps = data.get("steps")
    if not raw_steps:
        raise FlowError("設定檔沒有任何 [[steps]]")

    guards_raw = data.get("guards", {})
    guards = Guards(
        max_price=guards_raw.get("max_price"),
        stop_before_payment=bool(guards_raw.get("stop_before_payment", True)),
        dry_run=bool(guards_raw.get("dry_run", True)),
        max_attempts=int(guards_raw.get("max_attempts", 1)),
    )
    return Flow(steps=[_parse_step(s, i) for i, s in enumerate(raw_steps)], guards=guards)


def load_flow(path: Path | str) -> Flow:
    path = Path(path)
    if not path.is_file():
        raise FlowError(f"找不到流程設定檔 {path}（可從 flow.example.toml 複製一份）")
    with path.open("rb") as fh:
        return parse_flow(tomllib.load(fh))
