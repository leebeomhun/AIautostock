"""백테스트 KRX 규칙(매도세 0.20%, 상한가 종가 매수 미체결)과 정의 불가 지표 None·결과 경고."""

import json
from dataclasses import asdict

import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import StrategyIn, create_strategy
from app.config import Settings
from app.main import create_app
from app.models import PaperOrder
from app.quant import krx
from app.quant.backtest import BacktestParams, Sleeve, run_backtest, run_portfolio_backtest
from app.quant.signals import indicators


def test_krx_tick_and_limit_up_rules():
    assert [krx.tick_size(p) for p in (1_999, 2_000, 19_990, 20_000, 199_500, 500_000)] == [1, 5, 10, 50, 100, 1_000]
    assert krx.limit_up_price(9_980) == 12_970  # KRX 예시: 2,994 → 2,990 → 12,970
    assert krx.limit_up_price(10_000) == 13_000 and krx.limit_up_price(1_000) == 1_300
    assert krx.at_limit_up(13_000, 10_000) and not krx.at_limit_up(12_900, 10_000)
    assert krx.at_limit_up(129.6, 100.0)  # 수정주가처럼 호가 단위에 안 맞는 값
    assert not krx.at_limit_up(13_000, 10_000, limit=0) and not krx.at_limit_up(13_000, 0)


def test_sell_tax_defaults_to_2026_rate(container):
    assert krx.SELL_TAX_RATE == 0.002
    assert BacktestParams(template="momentum", amount=1, stop_pct=5, take_pct=10, max_weight_pct=15).sell_tax_rate == 0.002
    s = Settings(_env_file=None)
    assert s.sell_tax_rate == 0.002 and s.backtest_price_limit == 0.3
    # 로컬 시뮬레이션 계좌도 같은 세율로 매도 세금을 뗍니다.
    c = container
    assert c.broker.place_market_order("005930", "buy", 10).accepted
    assert c.broker.place_market_order("005930", "sell", 10).accepted
    with c.session_factory() as db:
        sold = db.scalars(select(PaperOrder).where(PaperOrder.side == "sell")).one()
        assert sold.tax == round(sold.fill_price * 10 * 0.002)


def _frame(last_jump: float, days: int = 80) -> pd.DataFrame:
    closes = [90 + i * 10 / (days - 1) for i in range(days)]
    closes.append(closes[-1] * (1 + last_jump))
    idx = pd.bdate_range(end="2026-10-29", periods=len(closes))
    return indicators(pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000.0] * days + [3000.0]}, index=idx))


def _bench(index) -> pd.Series:
    return pd.Series([100 + i * 0.1 for i in range(len(index))], index=index)


def _params(**kw) -> BacktestParams:
    return BacktestParams(template="momentum", amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15, **({"years": 1} | kw))


def test_limit_up_close_is_not_filled_in_backtests():
    f = _frame(0.30)  # 마지막 날 전날 종가 +30% 상한가로 돌파
    bench = _bench(f.index)
    assert run_backtest(_params(price_limit=0.0), {"A": f}, bench).orders == 1  # 끄면 예전처럼 종가 체결
    r = run_backtest(_params(), {"A": f}, bench)
    assert r.orders == 0 and any("상한가로 마감한 날의 매수 신호 1건" in w for w in r.warnings)
    assert run_backtest(_params(), {"A": _frame(0.10)}, _bench(_frame(0.10).index)).orders == 1  # +10% 돌파는 체결
    sleeves = [Sleeve("momentum", 1.0, 5, 10, 15)]
    assert run_portfolio_backtest(sleeves, {"A": f}, bench, years=1, price_limit=0.0).orders == 1
    port = run_portfolio_backtest(sleeves, {"A": f}, bench, years=1)
    assert port.orders == 0 and any("상한가" in w for w in port.warnings)


def test_undefined_metrics_are_none_and_json_safe():
    flat = _frame(0.0)  # 돌파 없음 → 매매 없음, 평가금액 변동 없음
    bench = _bench(flat.index)
    bench.iloc[:30] = float("nan")  # 앞부분 지수 시세 없음
    r = run_backtest(_params(), {"A": flat}, bench)
    assert r.ok and r.trades == 0 and r.win_rate is None and r.sharpe is None and r.volatility == 0.0
    assert any("승률을 계산하지 않았습니다" in w for w in r.warnings)
    assert r.curve[0]["benchmark"] is None and r.benchmark_return is not None
    json.dumps(asdict(r), allow_nan=False)  # NaN 이 나가면 API 응답이 깨집니다
    one_day = run_backtest(_params(years=0.001), {"A": flat}, bench.iloc[-1:])
    assert one_day.ok and one_day.cagr is None and one_day.volatility is None and one_day.sharpe is None
    no_bench = run_backtest(_params(), {"A": flat}, pd.Series(float("nan"), index=flat.index))
    assert no_bench.benchmark_return is None
    json.dumps(asdict(no_bench), allow_nan=False)


def test_backtest_warns_about_short_or_late_data():
    full = _frame(0.10, days=200)
    late = _frame(0.10, days=90)  # 같은 날 끝나지만 200일 구간 중간부터 시작
    short = full.iloc[-50:]
    r = run_backtest(BacktestParams(template="momentum", amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15, years=3), {"A": full, "B": late, "C": short}, _bench(full.index))
    text = " / ".join(r.warnings)
    assert "80일보다 짧은 1종목은 뺐습니다 (C)" in text
    assert "요청한 3년 중 지수 시세가 있는 0.8년만" in text
    assert "1종목은 시세가 백테스트 시작일" in text and "(B)" in text


def test_backtest_api_reports_unused_config_keys(settings, container):
    body = {"template": "momentum", "universe": "k200", "custom_symbols": ["005930"], "amount": 5_000_000, "stop_pct": 5, "take_pct": 10, "max_weight_pct": 15}
    with TestClient(create_app(settings, container)) as http:
        res = http.post("/api/backtest", json=body | {"daily_loss_limit": 300_000, "exclude_risky": True, "name": "x", "exec_mode": "auto", "stop_pcnt": 3}).json()
        assert res["ok"]
        text = " / ".join(res["warnings"])
        assert "알 수 없는 설정 키라 무시했습니다: stop_pcnt" in text
        assert "백테스트에 반영하지 않은 설정: 일일 손실 한도, 투자유의 종목 제외" in text
        assert "custom_symbols 는 쓰지 않았습니다" in text
        assert "name" not in text and "exec_mode" not in text
        clean = http.post("/api/backtest", json=body | {"custom_symbols": []}).json()
        assert not any("설정" in w for w in clean["warnings"])
    # 전략 생성은 모르는 키를 예전처럼 조용히 버립니다.
    sid = create_strategy(container, StrategyIn(**(body | {"custom_symbols": [], "stop_pcnt": 3})))["id"]
    assert sid
