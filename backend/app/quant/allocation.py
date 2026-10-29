"""시장 국면에 따른 전략 배분.

KOSPI 가 60일선 아래(signals.weak_market)인 약세장에서는 모멘텀 배분의 일부를 평균회귀로 옮깁니다.
배분 합계는 바뀌지 않아 전체 노출이 늘지 않습니다. 받을 평균회귀 전략이 없으면 아무것도 옮기지 않습니다
(모멘텀은 약세장에서 이미 종목당 매수 금액을 줄입니다 — signals.WEAK_MARKET_SIZE).
배분을 줄여도 보유 종목은 팔지 않고, 평가금액이 줄어든 배분을 넘는 동안 신규 매수만 멈춥니다.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable

GIVER_TEMPLATES = ("momentum",)
RECEIVER_TEMPLATES = ("meanrev",)


def regime_allocations(items: Iterable[tuple[Hashable, str, float]], weak: bool, shift: float) -> dict[Hashable, float]:
    """(키, 전략 템플릿, 배분) 목록 → 키별 실제로 쓸 배분. 약세장이면 모멘텀 배분의 shift(0–1) 비율을 평균회귀에 배분 비율대로 나눠 줍니다."""
    rows = list(items)
    out = {key: float(amount) for key, _template, amount in rows}
    shift = min(max(shift, 0.0), 1.0)
    receivers = [(key, float(amount)) for key, template, amount in rows if template in RECEIVER_TEMPLATES and amount > 0]
    if not weak or shift <= 0 or not receivers:
        return out
    moved = 0.0
    for key, template, amount in rows:
        if template in GIVER_TEMPLATES:
            out[key] = float(amount) * (1 - shift)
            moved += float(amount) * shift
    total = sum(amount for _key, amount in receivers)
    for key, amount in receivers:
        out[key] = amount + moved * amount / total
    return out
