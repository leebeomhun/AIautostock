"""투자 대상 종목 범위.

지수 구성 종목 전체가 아니라 시가총액 상위 일부만 담은 목록입니다. 키움 계좌는 국내 주식만 주문하므로
S&P500 은 로컬 시뮬레이션에서만 고를 수 있습니다. base_price 는 시뮬레이션 출발 가격일 뿐 실제 시세가 아닙니다.
키움에 연결되면 엔진이 코스피200 을 시가총액 상위 종목(K200_UNIVERSE_SIZE)으로 넓히고, 종목 리스트(ka10099)의
이름·업종을 register 로 더합니다.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.market.base import Instrument

KOSPI200 = [
    Instrument("005930", "삼성전자", "KOSPI", "반도체", 74_000),
    Instrument("000660", "SK하이닉스", "KOSPI", "반도체", 198_000),
    Instrument("373220", "LG에너지솔루션", "KOSPI", "2차전지", 370_000),
    Instrument("207940", "삼성바이오로직스", "KOSPI", "바이오", 980_000),
    Instrument("005380", "현대차", "KOSPI", "자동차", 245_000),
    Instrument("000270", "기아", "KOSPI", "자동차", 102_000),
    Instrument("068270", "셀트리온", "KOSPI", "바이오", 185_000),
    Instrument("035420", "NAVER", "KOSPI", "인터넷", 186_000),
    Instrument("035720", "카카오", "KOSPI", "인터넷", 42_000),
    Instrument("105560", "KB금융", "KOSPI", "금융", 82_000),
    Instrument("055550", "신한지주", "KOSPI", "금융", 52_000),
    Instrument("086790", "하나금융지주", "KOSPI", "금융", 63_000),
    Instrument("005490", "POSCO홀딩스", "KOSPI", "철강", 330_000),
    Instrument("051910", "LG화학", "KOSPI", "화학", 310_000),
    Instrument("006400", "삼성SDI", "KOSPI", "2차전지", 290_000),
    Instrument("012330", "현대모비스", "KOSPI", "자동차", 245_000),
    Instrument("028260", "삼성물산", "KOSPI", "지주", 130_000),
    Instrument("066570", "LG전자", "KOSPI", "전자", 95_000),
    Instrument("009150", "삼성전기", "KOSPI", "전자", 140_000),
    Instrument("017670", "SK텔레콤", "KOSPI", "통신", 52_000),
    Instrument("030200", "KT", "KOSPI", "통신", 41_000),
    Instrument("015760", "한국전력", "KOSPI", "유틸리티", 21_000),
]

KOSDAQ150 = [
    Instrument("247540", "에코프로비엠", "KOSDAQ", "2차전지", 180_000),
    Instrument("086520", "에코프로", "KOSDAQ", "2차전지", 90_000),
    Instrument("196170", "알테오젠", "KOSDAQ", "바이오", 320_000),
    Instrument("028300", "HLB", "KOSDAQ", "바이오", 70_000),
    Instrument("403870", "HPSP", "KOSDAQ", "반도체", 32_000),
    Instrument("058470", "리노공업", "KOSDAQ", "반도체", 200_000),
    Instrument("039030", "이오테크닉스", "KOSDAQ", "반도체", 160_000),
    Instrument("357780", "솔브레인", "KOSDAQ", "반도체", 250_000),
    Instrument("263750", "펄어비스", "KOSDAQ", "게임", 38_000),
    Instrument("293490", "카카오게임즈", "KOSDAQ", "게임", 18_000),
    Instrument("041510", "에스엠", "KOSDAQ", "엔터", 80_000),
    Instrument("035900", "JYP Ent.", "KOSDAQ", "엔터", 60_000),
]

SP500 = [
    Instrument("AAPL", "Apple", "US", "IT", 310_000),
    Instrument("MSFT", "Microsoft", "US", "IT", 580_000),
    Instrument("NVDA", "NVIDIA", "US", "반도체", 190_000),
    Instrument("AMZN", "Amazon", "US", "소비재", 270_000),
    Instrument("GOOGL", "Alphabet", "US", "인터넷", 240_000),
    Instrument("META", "Meta", "US", "인터넷", 800_000),
    Instrument("JPM", "JPMorgan", "US", "금융", 330_000),
    Instrument("V", "Visa", "US", "금융", 450_000),
]

UNIVERSES: dict[str, dict] = {
    "k200": {"label": "코스피200", "instruments": KOSPI200, "domestic": True},
    "kq150": {"label": "코스닥150", "instruments": KOSDAQ150, "domestic": True},
    "sp500": {"label": "미국 S&P500", "instruments": SP500, "domestic": False},
    "custom": {"label": "직접 고르기", "instruments": [], "domestic": True},
}

ALL_INSTRUMENTS: dict[str, Instrument] = {i.symbol: i for u in (KOSPI200, KOSDAQ150, SP500) for i in u}


def register(items: Iterable[Instrument]) -> None:
    """시세 소스가 알려 준 종목 이름·업종으로 덮어씁니다. 위 목록(시뮬레이션 출발 가격)은 바꾸지 않습니다."""
    for item in items:
        ALL_INSTRUMENTS[item.symbol] = item


def instrument(symbol: str) -> Instrument:
    found = ALL_INSTRUMENTS.get(symbol)
    if found:
        return found
    return Instrument(symbol, symbol, "KOSPI", "기타", 50_000)


def universe_symbols(universe: str, custom: list[str] | None = None) -> list[str]:
    if universe == "custom":
        return [s for s in (custom or []) if s]
    return [i.symbol for i in UNIVERSES.get(universe, UNIVERSES["k200"])["instruments"]]


def strategy_symbols(universe: str, custom: list[str] | None, market: object, k200_size: int) -> list[str]:
    """전략이 실제로 보는 종목. 코스피200 은 시세 소스가 알려 주는 시가총액 상위 k200_size 종목을 더합니다.

    키움(top_by_market_cap)만 지원하고 시뮬레이션은 기본 목록 그대로입니다. 조회가 실패하면 예외를 그대로 올립니다.
    """
    symbols = universe_symbols(universe, custom)
    top = getattr(market, "top_by_market_cap", None)
    if universe != "k200" or k200_size <= 0 or top is None:
        return symbols
    return sorted(set(symbols) | set(top(k200_size)))


def universe_label(universe: str) -> str:
    return UNIVERSES.get(universe, UNIVERSES["k200"])["label"]
