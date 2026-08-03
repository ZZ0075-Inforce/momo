"""命令列介面。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from .client import FetchError, MomoClient
from .config import Config
from .models import Availability, normalize_code
from .notifier import build_handlers
from .store import Store
from .text import pad
from .watcher import Watcher


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _make_client(config: Config) -> MomoClient:
    return MomoClient(
        timeout=config.http_timeout,
        concurrency=config.concurrency,
        min_request_gap=config.min_request_gap,
    )


# --- 指令 ---------------------------------------------------------------


def cmd_add(args: argparse.Namespace, config: Config) -> int:
    code = normalize_code(args.target)  # 網址解析失敗由 main() 統一處理
    store = Store(config.db_path)
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
        f"{pad('商品編號', 12)} {pad('狀態', 14)} "
        f"{pad('價格', 10, align='>')} {pad('目標價', 10, align='>')}  名稱"
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
            f"{pad(watch.code, 12)} {pad(status, 14)} "
            f"{pad(price, 10, align='>')} {pad(threshold, 10, align='>')}  {name}"
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

    if snapshot.looks_missing:
        # momo 對不存在的編號回 HTTP 200 加通用殼頁，所以這裡不是抓取失敗。
        print(f"查無此商品：{code}", file=sys.stderr)
        print("      momo 對不存在／已下架的編號會回一個沒有商品資訊的頁面。", file=sys.stderr)
        print(f"      先用瀏覽器開 {snapshot.desktop_url} 確認編號是否正確。", file=sys.stderr)
        return 3

    print(f"商品編號 : {snapshot.code}")
    print(f"名稱     : {snapshot.name or '(解析不到)'}")
    print(f"價格     : {f'NT${snapshot.price:,}' if snapshot.price is not None else '(解析不到)'}")
    print(f"庫存     : {snapshot.availability.value}")
    print(f"連結     : {snapshot.desktop_url}")
    if snapshot.availability is Availability.UNKNOWN:
        # 頁面有商品資訊卻讀不出庫存 —— 這才是 momo 改版的訊號。
        print("\n注意：抓到商品但庫存判定為 unknown，可能是 momo 改版。", file=sys.stderr)
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

    handlers = build_handlers(config)
    session = None

    if args.buy:
        # 監控一啟動就把瀏覽器開好、登入態載入、目標網域先連過一次。
        # 冷啟動一個 browser 要 2–3 秒，等偵測到補貨才開就已經輸了。
        session, purchase = await _build_purchase_handler(config, store)
        handlers.append(purchase)

    try:
        async with _make_client(config) as client:
            watcher = Watcher(config, store, client, handlers)
            if args.once:
                await watcher.run_once()
            else:
                await watcher.run_forever(stop)
    finally:
        if session is not None:
            await session.close()
        store.close()
    return 0


# --- Phase 2：下單流程 --------------------------------------------------


async def _build_purchase_handler(config: Config, store: Store):
    """開好預熱的瀏覽器 session，組出 PurchaseHandler。"""
    from .browser import BrowserSession
    from .flow import load_flow
    from .notifier import build_sender
    from .purchase import PurchaseHandler

    flow = load_flow(config.flow_path)
    session = BrowserSession(storage_state=config.storage_state, headless=config.headless)
    await session.start()
    await session.warm()

    mode = "演練" if flow.guards.dry_run else "實際下單"
    log = logging.getLogger(__name__)
    log.warning("自動下單已啟用（%s），流程設定：%s", mode, config.flow_path)
    if flow.has_placeholders:
        log.error("%s 仍有 TODO 佔位符，下單流程會被閘門擋下", config.flow_path)

    handler = PurchaseHandler(
        flow,
        session,
        store,
        build_sender(config),
        screenshot_dir=config.screenshot_dir,
    )
    return session, handler


async def _login(args: argparse.Namespace, config: Config) -> int:
    """開一個有畫面的瀏覽器讓你手動登入，然後把 cookie 存起來。

    刻意不自動化登入：momo 有簡訊 OTP 與圖形驗證，自動化既不可靠也不該做。
    登入一次存下 storage_state，之後所有流程重複使用。
    """
    from .browser import MOMO_HOME, BrowserSession

    session = BrowserSession(storage_state=config.storage_state, headless=False)
    await session.start()
    try:
        page = await session.new_page()
        await page.goto(MOMO_HOME, wait_until="domcontentloaded")
        print("瀏覽器已開啟。請在視窗中完成登入（含簡訊 OTP）。")
        print("登入完成後回到這裡按 Enter 儲存登入狀態……")
        await asyncio.to_thread(input)
        path = await session.save_login_state()
        print(f"登入狀態已存到 {path}")
        print("⚠️ 這個檔案等同你的登入憑證，不要提交進 git（.gitignore 已排除）。")
    finally:
        await session.close()
    return 0


async def _execute_flow(config: Config, code: str, *, dry_run: bool | None) -> int:
    from .browser import BrowserSession
    from .flow import FlowError, load_flow
    from .runner import FlowRunner

    try:
        flow = load_flow(config.flow_path)
    except FlowError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    effective_dry_run = flow.guards.dry_run if dry_run is None else dry_run
    if flow.has_placeholders:
        print(
            f"錯誤：{config.flow_path} 還有 TODO 佔位符，請先填入真正的 selector。", file=sys.stderr
        )
        return 1

    session = BrowserSession(storage_state=config.storage_state, headless=config.headless)
    await session.start()
    try:
        if not session.has_login_state:
            print("警告：沒有登入狀態，購物車與結帳步驟預期會失敗。", file=sys.stderr)
            print("      先跑 `momo-watch login`。", file=sys.stderr)
        await session.warm()
        page = await session.new_page()
        result = await FlowRunner(flow, dry_run=effective_dry_run).run(page, {"code": code})
    finally:
        await session.close()

    print(f"\n模式：{'演練（不會真的下單）' if effective_dry_run else '實際執行'}")
    print(result.summary())

    if result.ok:
        if result.stopped_for_dry_run:
            print("\n✅ 演練通過。要驗證購物車與結帳的 selector，")
            print("   把 flow.toml 的 dry_run 設成 false 再跑一次 ——")
            print("   stop_before_payment 仍然會讓流程停在付款前，不會扣款。")
        elif not effective_dry_run:
            print("\n✅ 已走到付款頁，請手動完成最後一步。")
        return 0

    failed = result.failed_step
    print(f"\n❌ 卡在「{failed.name}」：{failed.detail}", file=sys.stderr)
    print(f"   最後完成的步驟：{result.reached}", file=sys.stderr)
    return 1


async def _buy(args: argparse.Namespace, config: Config) -> int:
    dry_run = True if args.dry_run else (False if args.live else None)
    return await _execute_flow(config, normalize_code(args.target), dry_run=dry_run)


async def _arm(args: argparse.Namespace, config: Config) -> int:
    """等到指定時刻再跑下單流程，適用於已知開賣時間的限時搶購。"""
    from datetime import datetime

    from .clock import ntp_offset, seconds_until

    try:
        target = datetime.fromisoformat(args.at)
    except ValueError:
        print(
            f"錯誤：--at 格式無效（{args.at!r}），範例：2026-08-11T00:00:00+08:00", file=sys.stderr
        )
        return 1
    if target.tzinfo is None:
        target = target.astimezone()
        print(f"提示：--at 未帶時區，視為本機時區 → {target.isoformat()}")

    offset = await ntp_offset(config.ntp_server)
    wait = seconds_until(target, now=datetime.now(target.tzinfo), offset=offset, lead=args.lead)

    if wait <= 0:
        print(f"目標時刻已過（{-wait:.1f} 秒前），立刻執行。")
    else:
        print(f"目標：{target.isoformat()}")
        print(f"時鐘偏差：{offset:+.3f} 秒　提前量：{args.lead} 秒")
        print(f"等待 {wait:.1f} 秒……（Ctrl-C 取消）")
        await asyncio.sleep(wait)

    dry_run = True if args.dry_run else (False if args.live else None)
    return await _execute_flow(config, normalize_code(args.target), dry_run=dry_run)


def cmd_attempts(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.db_path)
    rows = store.recent_attempts(limit=args.limit)
    if not rows:
        print("還沒有任何下單嘗試。")
    for row in rows:
        mode = "演練" if row["dry_run"] else "實際"
        elapsed = f"{row['total_ms']:.0f}ms" if row["total_ms"] is not None else "-"
        print(
            f"{row['created_at'][:19]}  {row['code']:<10} {mode:<4} "
            f"{row['outcome']:<18} {elapsed:>8}  {row['reached'] or row['detail'] or ''}"
        )
    store.close()
    return 0


# --- 進入點 -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="momo-watch",
        description=(
            "momo 購物網商品庫存監控與半自動下單（下單流程一律停在付款前，不會自動送出付款）"
        ),
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
    p_run.add_argument(
        "--buy",
        action="store_true",
        help="補貨時自動跑下單流程（依 flow.toml 的閘門設定，預設為演練）",
    )

    # --- Phase 2 ---
    sub.add_parser("login", help="開瀏覽器手動登入一次，儲存登入狀態")

    def add_mode_flags(p: argparse.ArgumentParser) -> None:
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="只驗證 selector，不真的操作")
        mode.add_argument(
            "--live", action="store_true", help="實際執行（仍會停在付款前，不會扣款）"
        )

    p_buy = sub.add_parser("buy", help="立刻對指定商品跑一次下單流程")
    p_buy.add_argument("target", help="momo 商品網址或 i_code")
    add_mode_flags(p_buy)

    p_arm = sub.add_parser("arm", help="等到指定時刻再跑下單流程（限時搶購用）")
    p_arm.add_argument("target", help="momo 商品網址或 i_code")
    p_arm.add_argument(
        "--at", required=True, help="開賣時刻，ISO 格式，例：2026-08-11T00:00:00+08:00"
    )
    p_arm.add_argument("--lead", type=float, default=0.5, help="提前幾秒開始跑流程（預設 0.5）")
    add_mode_flags(p_arm)

    p_attempts = sub.add_parser("attempts", help="列出最近的下單嘗試紀錄")
    p_attempts.add_argument("--limit", type=int, default=20)

    return parser


def _dispatch(args: argparse.Namespace, config: Config) -> int:
    if args.command == "add":
        return cmd_add(args, config)
    if args.command == "rm":
        return cmd_remove(args, config)
    if args.command == "list":
        return cmd_list(args, config)
    if args.command == "events":
        return cmd_events(args, config)
    if args.command == "attempts":
        return cmd_attempts(args, config)
    if args.command == "check":
        return asyncio.run(_check(args, config))
    if args.command == "run":
        return asyncio.run(_run(args, config))
    if args.command == "login":
        return asyncio.run(_login(args, config))
    if args.command == "buy":
        return asyncio.run(_buy(args, config))
    if args.command == "arm":
        return asyncio.run(_arm(args, config))
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.from_env()
    _setup_logging(config.log_level)

    try:
        return _dispatch(args, config)
    except ValueError as exc:
        # 多半是 normalize_code 收到看不懂的網址。使用者貼錯連結是常態，
        # 不該丟一整串 traceback 出去。
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
