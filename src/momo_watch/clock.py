"""校時。

整點開賣的場景下，本機時鐘漂移 1 秒就出局 —— 而容器 / VPS 的時鐘漂移個
幾百毫秒是常態。這裡用 SNTP 量出本機與授時伺服器的偏差，排程時扣掉。

刻意不引入 ntplib：需要的邏輯就這幾十行，而且拆成純函式才測得到。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from datetime import datetime

log = logging.getLogger(__name__)

#: NTP 紀元（1900-01-01）到 Unix 紀元（1970-01-01）的秒數差。
NTP_EPOCH_DELTA = 2_208_988_800

#: 國家時間與頻率標準實驗室的授時伺服器。
DEFAULT_NTP_SERVER = "time.stdtime.gov.tw"


def build_request() -> bytes:
    """組一個最小的 SNTP client 封包。

    第一個 byte 是 LI(2) | VN(3) | Mode(3) = 0b00_011_011 = 0x1B，
    也就是「無閏秒警告、NTP 版本 3、client 模式」。
    """
    packet = bytearray(48)
    packet[0] = 0x1B
    return bytes(packet)


def parse_transmit_timestamp(data: bytes) -> float:
    """從 SNTP 回應取出 transmit timestamp，回傳 Unix 時間（秒）。"""
    if len(data) < 48:
        raise ValueError(f"SNTP 回應長度不足：{len(data)} bytes")
    seconds, fraction = struct.unpack("!II", data[40:48])
    if seconds == 0:
        raise ValueError("SNTP 回應的 transmit timestamp 是 0")
    return seconds + fraction / 2**32 - NTP_EPOCH_DELTA


def _query_sync(host: str, timeout: float) -> float:
    """送一次 SNTP 查詢，回傳 offset（秒）= 伺服器時間 - 本機時間。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sent_at = time.time()
        sock.sendto(build_request(), (host, 123))
        data, _ = sock.recvfrom(512)
        received_at = time.time()

    server_time = parse_transmit_timestamp(data)
    # 假設來回延遲對稱，用中點當作本機對應時刻。
    local_midpoint = (sent_at + received_at) / 2
    return server_time - local_midpoint


async def ntp_offset(host: str = DEFAULT_NTP_SERVER, *, timeout: float = 5.0) -> float:
    """量測本機時鐘偏差（秒）。失敗回 0.0 —— 校不到時鐘不該讓排程整個放棄。"""
    try:
        offset = await asyncio.to_thread(_query_sync, host, timeout)
    except (OSError, ValueError) as exc:
        log.warning("校時失敗（%s），以本機時鐘為準：%s", host, exc)
        return 0.0

    log.info("校時完成：本機比 %s %s %.3f 秒", host, "快" if offset < 0 else "慢", abs(offset))
    return offset


def seconds_until(
    target: datetime,
    *,
    now: datetime,
    offset: float = 0.0,
    lead: float = 0.0,
) -> float:
    """距離目標時刻還有幾秒，已扣掉時鐘偏差與提前量。

    純函式，方便測試。回傳值可能是負的（代表已經過了目標時刻）。

    offset 是「伺服器時間 - 本機時間」：offset 為正代表本機慢了，
    所以真正該等的時間要再短一點，因此是減去 offset。
    """
    if target.tzinfo is None or now.tzinfo is None:
        raise ValueError("target 與 now 都必須帶時區")
    return (target - now).total_seconds() - offset - lead
