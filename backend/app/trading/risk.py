"""리스크 가드. AI 판단과 상관없이 주문 직전에 항상 적용됩니다.

일일 손실 한도는 단계별로 동작합니다 (온보딩 화면의 '이렇게 지켜드려요').
- 50%: 알림
- 80%: 신규 매수 금액을 절반으로
- 100%: limit_action=buy 면 신규 매수 중지(손절·익절은 계속), all 이면 모든 매매 중지 + 미체결 취소
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LossState:
    used: float  # 오늘 손실 금액 (양수)
    limit: float
    action: str  # buy | all

    @property
    def ratio(self) -> float:
        return self.used / self.limit if self.limit > 0 else 0.0

    @property
    def level(self) -> int:
        r = self.ratio
        if r >= 1:
            return 100
        if r >= 0.8:
            return 80
        if r >= 0.5:
            return 50
        return 0

    @property
    def buys_halted(self) -> bool:
        return self.level >= 100

    @property
    def all_halted(self) -> bool:
        return self.level >= 100 and self.action == "all"


@dataclass
class BuyCheck:
    qty: int
    blocked: bool
    reason: str | None = None
    notes: list[str] = field(default_factory=list)


def size_buy(
    price: float,
    allocation: float,
    strategy_invested: float,
    position_value: float,
    max_weight_pct: float,
    cash: float,
    fee_rate: float,
) -> int:
    """종목당 최대 비중과 전략 배분, 주문 가능 현금 안에서 살 수 있는 수량."""
    if price <= 0:
        return 0
    per_symbol_cap = allocation * max_weight_pct / 100 - position_value
    strategy_room = allocation - strategy_invested
    budget = min(per_symbol_cap, strategy_room, cash / (1 + fee_rate))
    return max(0, int(budget // price))


def check_buy(
    qty: int,
    price: float,
    loss: LossState,
    orders_today: int,
    max_orders_per_day: int,
    strategy_loss_today: float,
    strategy_loss_limit: float,
    position_value: float,
    allocation: float,
    max_weight_pct: float,
) -> BuyCheck:
    notes: list[str] = []
    if loss.buys_halted:
        return BuyCheck(0, True, "일일 손실 한도에 도달해 신규 매수를 멈췄습니다")
    if orders_today >= max_orders_per_day:
        return BuyCheck(0, True, f"하루 최대 주문 수({max_orders_per_day}회)에 도달했습니다")
    if strategy_loss_limit > 0 and strategy_loss_today >= strategy_loss_limit:
        return BuyCheck(0, True, "이 전략의 일일 손실 한도에 도달했습니다")
    cap_value = allocation * max_weight_pct / 100
    if position_value >= cap_value * 0.999:
        return BuyCheck(0, True, f"종목당 최대 비중 {max_weight_pct:.0f}%에 도달해 주문을 내지 않았습니다")
    if loss.level >= 80:
        qty = qty // 2
        notes.append("일일 손실 한도 80% 사용 · 매수 금액 절반")
    if qty <= 0:
        return BuyCheck(0, True, "주문 가능한 수량이 없습니다 (현금 또는 비중 한도)")
    return BuyCheck(qty, False, None, notes)


def check_sell(loss: LossState, orders_today: int, max_orders_per_day: int, mandatory: bool) -> str | None:
    if loss.all_halted:
        return "일일 손실 한도 도달로 모든 매매를 멈췄습니다"
    # 손절은 하루 주문 수 한도와 상관없이 냅니다.
    if not mandatory and orders_today >= max_orders_per_day:
        return f"하루 최대 주문 수({max_orders_per_day}회)에 도달했습니다"
    return None
