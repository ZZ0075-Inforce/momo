"""校時。只測純函式 —— 真的打 NTP 伺服器的部分在 CI 裡沒意義也不穩。"""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from momo_watch.clock import (
    NTP_EPOCH_DELTA,
    build_request,
    parse_transmit_timestamp,
    seconds_until,
)


def make_response(unix_time: float) -> bytes:
    """組一個把 transmit timestamp 設成指定 Unix 時間的 SNTP 回應。"""
    ntp_time = unix_time + NTP_EPOCH_DELTA
    seconds = int(ntp_time)
    fraction = int((ntp_time - seconds) * 2**32)
    return bytes(40) + struct.pack("!II", seconds, fraction)


class TestRequest:
    def test_packet_is_48_bytes(self):
        assert len(build_request()) == 48

    def test_first_byte_is_client_mode_version_3(self):
        # LI=0(00) VN=3(011) Mode=3(011) -> 0b00011011
        assert build_request()[0] == 0x1B

    def test_rest_of_packet_is_zeroed(self):
        assert build_request()[1:] == bytes(47)


class TestParseResponse:
    def test_roundtrip(self):
        assert parse_transmit_timestamp(make_response(1_700_000_000.0)) == pytest.approx(
            1_700_000_000.0, abs=1e-6
        )

    def test_fractional_seconds_survive(self):
        assert parse_transmit_timestamp(make_response(1_700_000_000.25)) == pytest.approx(
            1_700_000_000.25, abs=1e-6
        )

    def test_short_packet_rejected(self):
        with pytest.raises(ValueError, match="長度不足"):
            parse_transmit_timestamp(bytes(20))

    def test_zero_timestamp_rejected(self):
        """全零的回應代表伺服器沒填時間，拿來校時會把時鐘校到 1900 年。"""
        with pytest.raises(ValueError, match="是 0"):
            parse_transmit_timestamp(bytes(48))


class TestSecondsUntil:
    now = datetime(2026, 8, 10, 23, 59, 0, tzinfo=UTC)
    target = datetime(2026, 8, 11, 0, 0, 0, tzinfo=UTC)

    def test_plain_countdown(self):
        assert seconds_until(self.target, now=self.now) == 60.0

    def test_lead_time_fires_earlier(self):
        assert seconds_until(self.target, now=self.now, lead=2.0) == 58.0

    def test_slow_local_clock_shortens_the_wait(self):
        """offset > 0 代表本機比伺服器慢，所以實際要等的時間更短。"""
        assert seconds_until(self.target, now=self.now, offset=0.5) == 59.5

    def test_fast_local_clock_lengthens_the_wait(self):
        assert seconds_until(self.target, now=self.now, offset=-0.5) == 60.5

    def test_past_target_is_negative(self):
        past = self.now - timedelta(seconds=30)
        assert seconds_until(past, now=self.now) == -30.0

    def test_naive_datetime_rejected(self):
        """沒有時區的時間在台灣 / UTC 之間差 8 小時，寧可直接報錯。"""
        naive = datetime(2026, 8, 11, 0, 0, 0)
        with pytest.raises(ValueError, match="時區"):
            seconds_until(naive, now=self.now)
        with pytest.raises(ValueError, match="時區"):
            seconds_until(self.target, now=naive)

    def test_taipei_timezone_is_handled(self):
        """跨時區比較要正確。Windows 沒有系統時區資料庫，靠 tzdata 這個相依套件。"""
        taipei = datetime(2026, 8, 11, 8, 0, 0, tzinfo=UTC).astimezone(ZoneInfo("Asia/Taipei"))
        assert taipei.hour == 16  # UTC+8
        assert seconds_until(taipei, now=datetime(2026, 8, 11, 7, 59, 0, tzinfo=UTC)) == 60.0
