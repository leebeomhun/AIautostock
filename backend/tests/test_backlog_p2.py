"""백로그 P2: 모멘텀 52주 고점 필터, 기관·외국인 수급 일별 스냅샷."""

from datetime import datetime

import pandas as pd

from app.api.routes import StrategyIn, create_strategy
from app.config import Settings
from app.container import build_container
from app.db import now_kst
from app.market.flow_store import load_snapshots, save_snapshot
from app.ml.nightly import snapshot_flow
from app.quant import signals
from app.quant.signals import Candidate, Signal, describe_entry, entry_frame, indicators


def _long_breakout(peak: float) -> pd.DataFrame:
    """peak 까지 오른 뒤 100 근처로 내려와 쉬다가 마지막 날 거래량 3배로 20일 고점을 넘는 300일 일봉."""
    up = [60 + i * (peak - 60) / 99 for i in range(100)]
    down = [peak - i * (peak - 100) / 99 for i in range(100)]
    flat = [95 + i * 5 / 98 for i in range(99)]
    closes = up + down + flat + [102.0]
    idx = pd.bdate_range(end="2026-10-29", periods=len(closes))
    volume = [1000.0] * (len(closes) - 1) + [3000.0]
    return indicators(pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": volume}, index=idx))


def test_high52_filter_blocks_breakouts_far_below_52_week_high(monkeypatch):
    far, near = _long_breakout(200.0), _long_breakout(120.0)  # 52주 고점 대비 −49% / −15%
    assert signals.HIGH52_RANGE == 0.2
    assert not entry_frame("momentum", far)["entry"].iloc[-1]
    assert entry_frame("momentum", near)["entry"].iloc[-1]
    cand = describe_entry("momentum", "A", "A", near, None)
    assert any(x.key == "high52" and "52주 최고 종가" in x.detail for x in cand.signals)
    monkeypatch.setattr(signals, "HIGH52_RANGE", 0.0)
    assert entry_frame("momentum", far)["entry"].iloc[-1]  # 끄면 예전 규칙
    assert not any(x.key == "high52" for x in describe_entry("momentum", "A", "A", far, None).signals)


def test_high52_filter_skips_short_histories():
    closes = [90 + i * 10 / 79 for i in range(80)] + [102.0]
    idx = pd.bdate_range(end="2026-10-29", periods=len(closes))
    ind = indicators(pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000.0] * 80 + [3000.0]}, index=idx))
    assert ind["high250"].isna().all() and entry_frame("momentum", ind)["entry"].iloc[-1]


def test_high52_range_comes_from_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(signals, "HIGH52_RANGE", signals.HIGH52_RANGE)  # 테스트 뒤 되돌립니다
    build_container(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}", data_source="simulated", engine_enabled=False, momentum_high52_range=0.0, data_dir=tmp_path))
    assert signals.HIGH52_RANGE == 0.0


def test_flow_snapshot_store_keeps_labels(tmp_path):
    save_snapshot(tmp_path, {"005930": (4, 2)}, datetime(2026, 10, 29, 15, 5), "close_window")
    save_snapshot(tmp_path, {"005930": (5, 3), "000660": (-1, 2)}, datetime(2026, 10, 29, 18, 40), "eod")
    data = load_snapshots(tmp_path, "2026-10-29")
    assert set(data["snapshots"]) == {"close_window", "eod"}
    assert data["snapshots"]["close_window"]["rows"] == {"005930": [4, 2]}
    assert data["snapshots"]["eod"]["rows"]["000660"] == [-1, 2] and data["snapshots"]["eod"]["taken_at"].startswith("2026-10-29T18:40")


def test_engine_saves_close_window_flow_once_a_day(container, monkeypatch):
    c = container
    create_strategy(c, StrategyIn(template="momentum", universe="custom", custom_symbols=["005930", "000660"], amount=10_000_000, stop_pct=5, take_pct=10, max_weight_pct=15))
    monkeypatch.setattr("app.trading.engine.describe_entry", lambda template, symbol, name, ind, bench: Candidate(symbol, name, "buy", "breakout", 0.6, [Signal("x", "t", "t", "buy", 0.6)]))
    calls = {"n": 0}

    def flow():
        calls["n"] += 1
        return {"000660": (3, 2 + calls["n"])}

    monkeypatch.setattr(c.market, "investor_flow", flow, raising=False)
    c.engine._prepare(now_kst())  # noqa: SLF001
    c.engine._prepare(now_kst())  # noqa: SLF001
    snap = load_snapshots(c.settings.data_dir, now_kst().strftime("%Y-%m-%d"))
    assert calls["n"] == 2 and snap["snapshots"]["close_window"]["rows"] == {"000660": [3, 3]}  # 그날 처음 받은 목록


def test_nightly_saves_eod_flow_snapshot(tmp_path):
    class Market:
        def investor_flow(self):
            return {"005930": (2, 2)}

    class Broken:
        def investor_flow(self):
            raise RuntimeError("down")

    assert snapshot_flow(Market(), tmp_path) == 1
    assert load_snapshots(tmp_path, now_kst().strftime("%Y-%m-%d"))["snapshots"]["eod"]["rows"] == {"005930": [2, 2]}
    assert snapshot_flow(Broken(), tmp_path) == 0 and snapshot_flow(object(), tmp_path) == 0
