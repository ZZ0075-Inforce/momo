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

log = logging.getLogger(__name__)


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
    """開一個有畫面的瀏覽器讓你手動登入，偵測到登入完成就自動存檔。

    刻意不自動化登入本身：momo 有簡訊 OTP 與圖形驗證，自動化既不可靠也不該做。

    也刻意不用「按 Enter 繼續」：那需要互動式 stdin，透過工具或腳本呼叫時
    會直接 EOF 失敗。改成偵測你離開登入頁，並且每隔一段時間就存一次快照，
    所以就算你直接把瀏覽器關掉，登入狀態也已經落地了。
    """
    from .browser import LOGIN_URL, BrowserSession, on_login_page

    session = BrowserSession(storage_state=config.storage_state, headless=False)
    await session.start()

    try:
        page = await session.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

        print("瀏覽器已開啟，請在視窗中完成登入（含簡訊 OTP）。")
        print(f"偵測到登入完成會自動存到 {config.storage_state}，不需要回來按任何鍵。")
        print(f"（最多等 {args.timeout // 60} 分鐘；隨時可以直接關掉瀏覽器，之前的進度已存下）\n")

        deadline = asyncio.get_running_loop().time() + args.timeout
        saved = False

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2)

            if page.is_closed():
                print("瀏覽器已關閉。")
                break

            # 先存快照再判斷 —— 使用者隨時可能關視窗，存過就不怕白做工。
            try:
                await session.save_login_state()
                saved = True
            except Exception as exc:  # noqa: BLE001 - 視窗關閉時會失敗，屬正常
                log.debug("存檔失敗（多半是視窗剛關閉）：%s", exc)
                break

            if not on_login_page(page.url):
                print(f"偵測到已離開登入頁 → {page.url[:70]}")
                await asyncio.sleep(2)  # 讓 momo 把 cookie 都設完
                await session.save_login_state()
                print(f"\n✅ 登入狀態已存到 {config.storage_state}")
                print("⚠️ 這個檔案等同你的登入憑證，別提交、別放共用目錄。")
                print("\n下一步：python tools/probe_selectors.py --url <購物車網址>")
                return 0
        else:
            print(f"\n等待逾時（{args.timeout} 秒）。", file=sys.stderr)

        if saved:
            print(f"已存下最後一次快照到 {config.storage_state}。")
            print("用 `momo-watch whoami` 確認登入是否有效。")
            return 0

        print("沒有存到任何登入狀態。", file=sys.stderr)
        return 1
    finally:
        with contextlib.suppress(Exception):
            await session.close()


async def _whoami(args: argparse.Namespace, config: Config) -> int:
    """確認已存下的登入狀態還有沒有效。cookie 會過期，檔案不會自己消失。"""
    from .browser import BrowserSession

    session = BrowserSession(storage_state=config.storage_state, headless=config.headless)
    if not session.has_login_state:
        print(
            f"找不到 {config.storage_state}，尚未登入。先跑 `momo-watch login`。", file=sys.stderr
        )
        return 1

    await session.start()
    try:
        if await session.is_logged_in():
            print(f"✅ 登入有效（{config.storage_state}）")
            return 0
        print(
            f"❌ 登入已失效或過期（{config.storage_state}）。重跑 `momo-watch login`。",
            file=sys.stderr,
        )
        return 1
    finally:
        await session.close()


async def _execute_flow(config: Config, code: str, *, dry_run: bool | None) -> int:
    from .browser import BrowserSession
    from .flow import FlowError, load_flow, placeholder_steps
    from .runner import FlowRunner

    try:
        flow = load_flow(config.flow_path)
    except FlowError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    effective_dry_run = flow.guards.dry_run if dry_run is None else dry_run

    # 演練只跑到第一個 mutating 步驟，所以只檢查那段的 selector 有沒有填。
    # 這樣才能先填商品頁、驗證通過，再回頭填購物車與結帳 —— 後面那些步驟
    # 本來就要先把東西放進購物車才看得到。
    relevant = flow.dry_run_steps if effective_dry_run else flow.steps
    if pending := placeholder_steps(relevant):
        print(f"錯誤：{config.flow_path} 這些步驟還是 TODO 佔位符：", file=sys.stderr)
        for name in pending:
            print(f"        - {name}", file=sys.stderr)
        print("      填好真正的 selector 再跑。", file=sys.stderr)
        return 1

    if effective_dry_run and (later := placeholder_steps(flow.steps)):
        print(f"（演練不會碰到這 {len(later)} 個尚未填寫的步驟：{'、'.join(later)}）")

    session = BrowserSession(storage_state=config.storage_state, headless=config.headless)
    await session.start()
    try:
        # 只有真的要動購物車時才值得花一次往返去驗證登入；演練停在商品頁，
        # 那段本來就不需要登入。
        if not effective_dry_run:
            if not session.has_login_state:
                print("警告：沒有登入狀態，購物車與結帳步驟預期會失敗。", file=sys.stderr)
                print("      先跑 `momo-watch login`。", file=sys.stderr)
            elif not await session.is_logged_in():
                print("警告：登入狀態已失效或過期，購物車與結帳步驟預期會失敗。", file=sys.stderr)
                print("      重跑 `momo-watch login`。", file=sys.stderr)
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
    p_login = sub.add_parser("login", help="開瀏覽器手動登入一次，儲存登入狀態")
    p_login.add_argument("--timeout", type=int, default=600, help="最多等幾秒完成登入（預設 600）")

    sub.add_parser("whoami", help="確認已存下的登入狀態還有沒有效")

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
    if args.command == "whoami":
        return asyncio.run(_whoami(args, config))
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
