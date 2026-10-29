"""시장 국면에 따른 전략 배분.

KOSPI 가 60일선 아래(signals.weak_market)인 약세장에서 모멘텀 배분을 shift(0–1) 비율만큼 줄이고, 줄인 몫은 현금으로 둡니다.
다른 전략(평균회귀 등)으로 옮기지 않습니다 — 2018–22 장기 검증에서 약세장에 평균회귀로 옮긴 배분이 손실을 키웠습니다
(research/long_check.py). 기본값 REGIME_WEAK_SHIFT=0 이고, 모멘텀은 약세장에서 이미 신규 진입을 멈추므로
(signals.WEAK_MARKET_SIZE) 기본 설정에서는 이 함수가 배분을 바꾸지 않습니다.
배분을 줄여도 보유 종목은 팔지 않고, 평가금액이 줄어든 배분을 넘는 동안 신규 매수만 멈춥니다.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable

GIVER_TEMPLATES = ("momentum",)


def regime_allocations(items: Iterable[tuple[Hashable, str, float]], weak: bool, shift: float) -> dict[Hashable, float]:
    """(키, 전략 템플릿, 배분) 목록 → 키별 실제로 쓸 배분. 약세장이면 모멘텀 배분을 shift(0–1) 비율만큼 줄이고 나머지 전략은 그대로 둡니다."""
    rows = list(items)
    out = {key: float(amount) for key, _template, amount in rows}
    shift = min(max(shift, 0.0), 1.0)
    if not weak or shift <= 0:
        return out
    for key, template, amount in rows:
        if template in GIVER_TEMPLATES:
            out[key] = float(amount) * (1 - shift)
    return out
