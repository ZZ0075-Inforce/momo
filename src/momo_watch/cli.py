"""命令列介面。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import unicodedata

from .client import FetchError, MomoClient
from .config import Config
from .models import normalize_code
from .notifier import build_handlers
from .store import Store
from .watcher import Watcher


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _width(text: str) -> int:
    """終端機顯示寬度。中文字佔兩欄，str 的 len() 算不準。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, *, align: str = "<") -> str:
    padding = " " * max(0, width - _width(text))
    return padding + text if align == ">" else text + padding


def _make_client(config: Config) -> MomoClient:
    return MomoClient(
        timeout=config.http_timeout,
        concurrency=config.concurrency,
        min_request_gap=config.min_request_gap,
    )


# --- 指令 ---------------------------------------------------------------


def cmd_add(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.db_path)
    try:
        code = normalize_code(args.target)
    except ValueError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    store.add_watch(code, label=args.label, price_threshold=args.threshold)
    extra = f"，目標價 NT${args.threshold:,}" if args.threshold else ""
    print(f"已加入監控：{code}{extra}")
    store.close()
    return 0


def cmd_remove(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.db_path)
    code = normalize_code(args.target)
    print(f"已移除 {code}" if store.remove_watch(code) else f"清單裡沒有 {code}")
    store.close()
    return 0


def cmd_list(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.db_path)
    watches = store.list_watches()
    if not watches:
        print("監控清單是空的。用 `momo-watch add <網址或商品編號>` 加入商品。")
        store.close()
        return 0

    header = (
        f"{_pad('商品編號', 12)} {_pad('狀態', 14)} "
        f"{_pad('價格', 10, align='>')} {_pad('目標價', 10, align='>')}  名稱"
    )
    print(header)
    print("-" * 60)
    for watch in watches:
        state = store.get_state(watch.code)
        status = state.availability.value if state else "未檢查"
        price = f"NT${state.price:,}" if state and state.price is not None else "-"
        threshold = f"NT${watch.price_threshold:,}" if watch.price_threshold else "-"
        name = (watch.label or (state.name if state else None) or "")[:32]
        print(
            f"{_pad(watch.code, 12)} {_pad(status, 14)} "
            f"{_pad(price, 10, align='>')} {_pad(threshold, 10, align='>')}  {name}"
        )
    store.close()
    return 0


def cmd_events(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.db_path)
    rows = store.recent_events(limit=args.limit)
    if not rows:
        print("目前還沒有任何事件。")
    for row in rows:
        print(f"{row['created_at'][:19]}  {row['code']:<10} {row['kind']:<16} {row['detail']}")
    store.close()
    return 0


async def _check(args: argparse.Namespace, config: Config) -> int:
    """一次性查詢，不寫入資料庫 —— 用來驗證解析邏輯還沒被 momo 改版打壞。"""
    code = normalize_code(args.target)
    async with _make_client(config) as client:
        try:
            snapshot = await client.fetch(code)
        except FetchError as exc:
            print(f"抓取失敗：{exc}", file=sys.stderr)
            return 1

    print(f"商品編號 : {snapshot.code}")
    print(f"名稱     : {snapshot.name or '(解析不到)'}")
    print(f"價格     : {f'NT${snapshot.price:,}' if snapshot.price is not None else '(解析不到)'}")
    print(f"庫存     : {snapshot.availability.value}")
    print(f"連結     : {snapshot.desktop_url}")
    if snapshot.availability.value == "unknown":
        print("\n注意：庫存判定為 unknown，可能是 momo 改版或被擋。", file=sys.stderr)
        print("      把頁面存成 fixture 丟進 tests/ 重現，再修 parser.py。", file=sys.stderr)
        return 2
    return 0


async def _run(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.db_path)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 沒有 SIGTERM
            loop.add_signal_handler(sig, stop.set)

    async with _make_client(config) as client:
        watcher = Watcher(config, store, client, build_handlers(config))
        if args.once:
            await watcher.run_once()
        else:
            await watcher.run_forever(stop)
    store.close()
    return 0


# --- 進入點 -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="momo-watch",
        description="momo 購物網商品庫存 / 價格監控（Phase 1：只監控與通知，不下單）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="加入監控（吃 momo 網址或純商品編號）")
    p_add.add_argument("target", help="momo 商品網址或 i_code")
    p_add.add_argument("--label", help="自訂顯示名稱")
    p_add.add_argument("--threshold", type=int, help="目標價，跌破就推播")

    p_rm = sub.add_parser("rm", help="移除監控")
    p_rm.add_argument("target", help="momo 商品網址或 i_code")

    sub.add_parser("list", help="列出監控清單與最後狀態")

    p_events = sub.add_parser("events", help="列出最近的事件紀錄")
    p_events.add_argument("--limit", type=int, default=20)

    p_check = sub.add_parser("check", help="一次性查詢單一商品（不寫入資料庫）")
    p_check.add_argument("target", help="momo 商品網址或 i_code")

    p_run = sub.add_parser("run", help="啟動監控迴圈")
    p_run.add_argument("--once", action="store_true", help="只跑一輪就結束")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.from_env()
    _setup_logging(config.log_level)

    if args.command == "add":
        return cmd_add(args, config)
    if args.command == "rm":
        return cmd_remove(args, config)
    if args.command == "list":
        return cmd_list(args, config)
    if args.command == "events":
        return cmd_events(args, config)
    if args.command == "check":
        return asyncio.run(_check(args, config))
    if args.command == "run":
        return asyncio.run(_run(args, config))
    return 1


if __name__ == "__main__":
    sys.exit(main())
