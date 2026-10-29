"""KRX 휴장일 캘린더.

주말 말고도 공휴일·대체공휴일·선거일·연말(12/31)에는 정규장이 열리지 않습니다. 엔진은 휴장일에 판단·주문을 하지 않습니다.
목록은 COVERED_THROUGH 까지만 담겨 있고, 그 뒤 날짜는 휴장으로 봅니다 (fail closed — 목록을 갱신해야 다시 매매합니다).
KRX 가 매년 12월에 다음 해 휴장일을 공지하면 확인해서 고치고, 갑자기 정해진 임시공휴일은 KRX_EXTRA_HOLIDAYS 에 넣습니다.
"""

from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

KRX_HOLIDAYS: dict[date, str] = {
    # 2026
    date(2026, 1, 1): "신정",
    date(2026, 2, 16): "설날 연휴",
    date(2026, 2, 17): "설날",
    date(2026, 2, 18): "설날 연휴",
    date(2026, 3, 2): "삼일절 대체공휴일",
    date(2026, 5, 1): "근로자의 날",
    date(2026, 5, 5): "어린이날",
    date(2026, 5, 25): "부처님오신날 대체공휴일",
    date(2026, 6, 3): "전국동시지방선거",
    date(2026, 8, 17): "광복절 대체공휴일",
    date(2026, 9, 24): "추석 연휴",
    date(2026, 9, 25): "추석",
    date(2026, 10, 5): "개천절 대체공휴일",
    date(2026, 10, 9): "한글날",
    date(2026, 12, 25): "성탄절",
    date(2026, 12, 31): "연말 휴장",
    # 2027
    date(2027, 1, 1): "신정",
    date(2027, 2, 8): "설날 연휴",
    date(2027, 2, 9): "설날 대체공휴일",
    date(2027, 3, 1): "삼일절",
    date(2027, 5, 5): "어린이날",
    date(2027, 5, 13): "부처님오신날",
    date(2027, 8, 16): "광복절 대체공휴일",
    date(2027, 9, 14): "추석 연휴",
    date(2027, 9, 15): "추석",
    date(2027, 9, 16): "추석 연휴",
    date(2027, 10, 4): "개천절 대체공휴일",
    date(2027, 10, 11): "한글날 대체공휴일",
    date(2027, 12, 27): "성탄절 대체공휴일",
    date(2027, 12, 31): "연말 휴장",
}
COVERED_THROUGH = date(2027, 12, 31)
OUT_OF_RANGE = "휴장일 캘린더 범위 밖 · 목록 갱신 필요"

_extra: dict[date, str] = {}
_warned: set[int] = set()


def set_extra_holidays(text: str | None) -> None:
    """KRX_EXTRA_HOLIDAYS ('2026-11-02,2026-11-03' 형식)를 임시 휴장일로 더합니다. 형식이 틀린 값은 건너뜁니다."""
    _extra.clear()
    for raw in (text or "").replace(";", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            _extra[date.fromisoformat(raw)] = "임시 휴장일"
        except ValueError:
            log.warning("KRX_EXTRA_HOLIDAYS 의 날짜 형식이 틀렸습니다: %s", raw)


def holiday_name(d: date) -> str | None:
    """휴장일이면 이유, 아니면 None. 주말은 보지 않습니다."""
    if d > COVERED_THROUGH:
        if d.year not in _warned:
            _warned.add(d.year)
            log.warning("%s 은 휴장일 캘린더(%s까지) 밖이라 휴장으로 봅니다. app/market/calendar.py 를 갱신하세요", d, COVERED_THROUGH)
        return OUT_OF_RANGE
    return _extra.get(d) or KRX_HOLIDAYS.get(d)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and holiday_name(d) is None
