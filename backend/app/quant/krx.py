"""KRX 거래 규칙: 매도 세금, 호가가격단위, 가격제한폭(상한가). 백테스트와 로컬 시뮬레이션 비용 가정에 씁니다.

- 매도 세금 0.20% (2026-01-01 이후 양도분): 코스피 증권거래세 0.05% + 농어촌특별세 0.15%, 코스닥 증권거래세 0.20%.
  근거: 증권거래세법 시행령 제5조(탄력세율, 2025년 세제 개편안에 따른 개정), 농어촌특별세법 제5조.
  2025년에는 코스피·코스닥 모두 0.15% 였고, 금융투자소득세 폐지 보완으로 0.05%p 올랐습니다. 세율은 자주 바뀌니 시점마다 확인하세요.
- 호가가격단위: 2023-01-25 부터 코스피·코스닥 통합 표 (KRX 규정 포털 RGL03010100).
- 가격제한폭 ±30% (2015-06-15 부터): 기준가격(보통 전날 종가) × 30% 를 기준가격의 호가 단위로 절사한 폭을 더하고,
  그 가격의 호가 단위로 다시 절사합니다. KRX 예시: 기준가격 9,980 → 2,994 → 2,990 → 상한가 12,970.

규칙 정리 출처: Vibe-Trading (https://github.com/HKUDS/Vibe-Trading) agent/backtest/engines/korea_equity.py 를 참고해 다시 썼습니다.
Copyright (c) 2026 Vibe-Trading Contributors. MIT License.
"""

from __future__ import annotations

SELL_TAX_RATE = 0.0020
PRICE_LIMIT = 0.30
# (이 가격 이상, 호가 단위)
TICKS: tuple[tuple[float, float], ...] = ((0, 1), (2_000, 5), (5_000, 10), (20_000, 50), (50_000, 100), (200_000, 500), (500_000, 1_000))
# 수정주가는 호가 단위에 맞지 않을 수 있어, 계산한 상한가보다 이만큼(가격 대비) 아래까지 상한가로 봅니다 (절사 두 번 ≤ 약 0.5%).
ADJUSTED_SLACK = 0.005


def tick_size(price: float) -> float:
    tick = TICKS[0][1]
    for floor, t in TICKS:
        if price >= floor:
            tick = t
    return tick


def _truncate(price: float, tick: float) -> float:
    return int(price / tick + 1e-9) * tick


def limit_up_price(base: float, limit: float = PRICE_LIMIT) -> float:
    band = max(_truncate(base * limit, tick_size(base)), tick_size(base))
    up = base + band
    return _truncate(up, tick_size(up))


def at_limit_up(close: float, prev_close: float, limit: float = PRICE_LIMIT) -> bool:
    """종가가 상한가인지. 상한가 종가에는 팔려는 물량이 거의 없어 종가 매수가 체결되지 않는다고 봅니다. limit 0 이면 끔."""
    if not limit or prev_close <= 0 or close <= 0:
        return False
    return close >= min(limit_up_price(prev_close, limit), prev_close * (1 + limit - ADJUSTED_SLACK))
