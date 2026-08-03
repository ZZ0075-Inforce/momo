"""環境變數設定。刻意不引入 pydantic —— 這個規模用不上。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: 輪詢間隔硬下限。低於這個值對 momo 是騷擾、對你是封鎖風險。
MIN_POLL_INTERVAL = 5.0


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必須是數字，收到 {raw!r}") from exc


def _env_int(key: str, default: int) -> int:
    return int(_env_float(key, float(default)))


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """極簡 .env 載入器，已存在的環境變數優先。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True, slots=True)
class Config:
    db_path: Path
    poll_interval: float
    poll_jitter: float
    concurrency: int
    min_request_gap: float
    http_timeout: float
    telegram_token: str | None
    telegram_chat_id: str | None
    log_level: str
    # --- Phase 2（下單流程）。有預設值，只做監控時不用管。---
    flow_path: Path = Path("flow.toml")
    storage_state: Path = Path("storage_state.json")
    screenshot_dir: Path = Path("screenshots")
    headless: bool = True
    ntp_server: str = "time.stdtime.gov.tw"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @classmethod
    def from_env(cls, dotenv: Path | None = Path(".env")) -> Config:
        if dotenv is not None:
            _load_dotenv(dotenv)

        interval = _env_float("MOMO_POLL_INTERVAL", 60.0)
        if interval < MIN_POLL_INTERVAL:
            # 夾住而不是報錯：使用者通常只是想「快一點」，不必因此中斷。
            interval = MIN_POLL_INTERVAL

        return cls(
            db_path=Path(os.environ.get("MOMO_DB_PATH", "momo_watch.db")),
            poll_interval=interval,
            poll_jitter=max(0.0, _env_float("MOMO_POLL_JITTER", 5.0)),
            concurrency=max(1, _env_int("MOMO_CONCURRENCY", 4)),
            min_request_gap=max(0.0, _env_float("MOMO_MIN_REQUEST_GAP", 0.5)),
            http_timeout=_env_float("MOMO_HTTP_TIMEOUT", 10.0),
            telegram_token=os.environ.get("MOMO_TELEGRAM_TOKEN") or None,
            telegram_chat_id=os.environ.get("MOMO_TELEGRAM_CHAT_ID") or None,
            log_level=os.environ.get("MOMO_LOG_LEVEL", "INFO").upper(),
            flow_path=Path(os.environ.get("MOMO_FLOW_PATH", "flow.toml")),
            storage_state=Path(os.environ.get("MOMO_STORAGE_STATE", "storage_state.json")),
            screenshot_dir=Path(os.environ.get("MOMO_SCREENSHOT_DIR", "screenshots")),
            headless=_env_bool("MOMO_HEADLESS", True),
            ntp_server=os.environ.get("MOMO_NTP_SERVER", "time.stdtime.gov.tw"),
        )
