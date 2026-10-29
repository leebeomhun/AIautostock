from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import pandas as pd


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str  # KOSPI | KOSDAQ | US
    sector: str
    base_price: float = 0  # 시뮬레이션 시작 가격 (원)


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: float
    volume: float
    at: datetime
    per: float | None = None
    pbr: float | None = None
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def change_pct(self) -> float:
        return (self.price / self.prev_close - 1) * 100 if self.prev_close else 0.0


@dataclass
class MarketStatus:
    open: bool
    label: str


class MarketData(Protocol):
    name: str

    def history(self, symbol: str, days: int = 800) -> pd.DataFrame:
        """일봉. index=날짜(DatetimeIndex), columns=open, high, low, close, volume. 장중이면 마지막 행이 오늘."""
        ...

    def quote(self, symbol: str) -> Quote: ...

    def benchmark_history(self, days: int = 800) -> pd.Series: ...

    def benchmark_level(self) -> float: ...

    def status(self, now: datetime) -> MarketStatus: ...

    def risky_symbols(self) -> set[str]:
        """관리종목·거래정지·투자유의 종목 코드."""
        ...

    def tick(self, now: datetime) -> None:
        """엔진 주기마다 호출. 시뮬레이션은 가격을 움직이고, 키움은 캐시를 비웁니다."""
        ...


def krx_session_status(now: datetime) -> MarketStatus:
    """KRX 정규장 09:00–15:30 (KST, 평일). 공휴일·연말 휴장일(app.market.calendar)과 캘린더 범위 밖 날짜는 닫힌 것으로 봅니다."""
    from app.market.calendar import holiday_name

    hhmm = now.strftime("%H:%M")
    if now.weekday() >= 5:
        return MarketStatus(False, f"주말 휴장 · {hhmm}")
    holiday = holiday_name(now.date())
    if holiday:
        return MarketStatus(False, f"휴장일 ({holiday}) · {hhmm}")
    minutes = now.hour * 60 + now.minute
    if 9 * 60 <= minutes < 15 * 60 + 30:
        return MarketStatus(True, f"정규장 운영 중 · {hhmm}")
    if minutes < 9 * 60:
        return MarketStatus(False, f"장 시작 전 · {hhmm}")
    return MarketStatus(False, f"장 마감 · {hhmm}")
