"""운영 점검: 모의 체결가와 그날 종가의 차이(괴리), 최근 ML 학습 결과가 판단에 반영되는지.

백테스트는 그날 종가에 체결된다고 가정하고, 실시간 엔진은 15:00–15:20 판단 구간에 시장가로 주문합니다.
그 차이가 백테스트 비용 가정(슬리피지)보다 크면 백테스트 성과를 그만큼 깎아 봐야 합니다.
실행: `uv run python -m app.ops [--days 30]` — 종가가 확정되는 15:40 뒤, 그리고 18:40 ML 배치 뒤에 봅니다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.container import build_market
from app.db import make_session_factory, now_kst, session_scope
from app.insights import Insights
from app.models import Order

CLOSE_SETTLED_MINUTES = 15 * 60 + 40


def fill_vs_close(s: Session, market: Any, since: datetime, now: datetime) -> list[dict[str, Any]]:
    """체결된 주문마다 체결 평균가와 그날 종가의 차이. cost_pct 가 양수면 종가보다 불리하게 체결된 것입니다."""
    orders = s.scalars(
        select(Order).where(Order.filled_qty > 0, Order.fill_price.is_not(None), Order.created_at >= since).order_by(Order.created_at)
    ).all()
    closes: dict[str, pd.Series] = {}
    rows: list[dict[str, Any]] = []
    for o in orders:
        day = (o.filled_at or o.created_at).date()
        if day == now.date() and now.hour * 60 + now.minute < CLOSE_SETTLED_MINUTES:
            continue  # 오늘 종가가 아직 확정되지 않았습니다
        if o.symbol not in closes:
            closes[o.symbol] = market.history(o.symbol, 60)["close"]
        close = closes[o.symbol].get(pd.Timestamp(day))
        if close is None or not close > 0:
            continue
        sign = 1 if o.side == "buy" else -1  # 매수는 비싸게, 매도는 싸게 체결될수록 손해
        rows.append({
            "date": day.isoformat(),
            "symbol": o.symbol,
            "name": o.name,
            "side": o.side,
            "qty": o.filled_qty,
            "fill": float(o.fill_price),
            "close": float(close),
            "cost_pct": round((float(o.fill_price) / float(close) - 1) * sign * 100, 3),
            "strategy_id": o.strategy_id,
            "at": o.created_at.strftime("%H:%M"),
        })
    return rows


def summarize(rows: list[dict[str, Any]], slippage_rate: float) -> dict[str, Any]:
    out: dict[str, Any] = {"count": len(rows), "assumed_pct": round(slippage_rate * 100, 3)}
    for side in ("buy", "sell"):
        xs = [r["cost_pct"] for r in rows if r["side"] == side]
        out[side] = {"count": len(xs), "avg_cost_pct": round(sum(xs) / len(xs), 3) if xs else None}
    allx = [r["cost_pct"] for r in rows]
    out["avg_cost_pct"] = round(sum(allx) / len(allx), 3) if allx else None
    return out


def ml_review(insights: Insights, today: Any) -> dict[str, Any]:
    status = insights.ml_status()
    if not status:
        return {"ok": False, "why": "ML 결과 파일이 없습니다"}
    m = status.get("metrics") or {}
    ok, why = insights.ml_gate(m)
    fresh = insights._fresh(status.get("as_of"), insights.ml_max_age_days, today)  # noqa: SLF001
    if not fresh:
        ok, why = False, f"{status.get('as_of')} 결과라 오래돼 쓰지 않습니다 · {why}"
    return {"ok": ok, "why": why, "as_of": status.get("as_of"), "model": status.get("model"), "symbols": status.get("count"), "fold_rank_ics": m.get("fold_rank_ics"), "valid_rank_ic": m.get("valid_rank_ic"), "random_null": m.get("random_null")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="최근 며칠의 체결을 볼지")
    args = parser.parse_args()
    settings = get_settings()
    now = now_kst()
    market, _client = build_market(settings)
    with session_scope(make_session_factory(settings.database_url)) as s:
        rows = fill_vs_close(s, market, now - timedelta(days=args.days), now)
    summary = summarize(rows, settings.slippage_rate)
    print(f"[체결가 vs 종가] 최근 {args.days}일 {summary['count']}건 · 백테스트 슬리피지 가정 {summary['assumed_pct']}%")
    for r in rows[-20:]:
        print(f"  {r['date']} {r['at']} {r['side']:<4} {r['symbol']} {r['name']} {r['qty']}주 체결 {r['fill']:,.0f} / 종가 {r['close']:,.0f} → 불리 {r['cost_pct']:+.2f}%")
    if summary["count"]:
        print(f"  평균 불리 {summary['avg_cost_pct']:+.3f}% (매수 {summary['buy']['avg_cost_pct']}, 매도 {summary['sell']['avg_cost_pct']})")
    ml = ml_review(Insights(settings.data_dir), now.date())
    print(f"[ML] {ml.get('as_of')} · {ml.get('symbols')}종목 · 구간별 순위 IC {ml.get('fold_rank_ics')} → 판단 반영 {'예' if ml['ok'] else '아니오'} ({ml['why']})")


if __name__ == "__main__":
    main()
