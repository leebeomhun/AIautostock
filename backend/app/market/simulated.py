"""키움 키가 없을 때 쓰는 결정적(시드 고정) 시뮬레이션 시세.

시장 공통 요인 + 업종 요인 + 개별 요인으로 일봉을 만들어 종목 간 상관이 생기게 합니다.
장중 가격은 엔진 주기마다 작은 랜덤워크로 움직입니다. 화면에는 항상 '모의 시세'로 표시됩니다.
"""

from __future__ import annotations

import hashlib
import math
import threading
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from app.db import now_kst
from app.market.base import MarketStatus, Quote, krx_session_status
from app.market.universe import ALL_INSTRUMENTS

BENCHMARK = "__KOSPI__"


def _seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _business_days(end: date, count: int) -> pd.DatetimeIndex:
    return pd.bdate_range(end=end, periods=count)


class SimulatedMarket:
    name = "simulated"

    def __init__(self, seed: int = 7, always_open: bool = True, history_days: int = 1100) -> None:
        self.seed = seed
        self.always_open = always_open
        self.history_days = history_days
        self._lock = threading.Lock()
        self._built_for: date | None = None
        self._daily: dict[str, pd.DataFrame] = {}
        self._intraday: dict[str, dict[str, float]] = {}
        self._rng = np.random.default_rng(_seed(seed, "intraday"))
        self._last_tick: datetime | None = None
        self._beta: dict[str, float] = {}

    # ---- 일봉 생성 -------------------------------------------------------
    def _build(self, today: date) -> None:
        # 오늘 이전 영업일까지의 일봉을 만들고, 오늘은 장중 가격으로 이어 붙입니다.
        idx = _business_days(today - timedelta(days=1), self.history_days)
        n = len(idx)
        mrng = np.random.default_rng(_seed(self.seed, "market"))
        market = mrng.normal(0.00025, 0.0095, n)
        # 변동성 국면: 가끔 변동성이 커지는 구간을 섞습니다.
        regime = 1 + 0.9 * (np.sin(np.arange(n) / 37.0) > 0.85)
        market = market * regime

        sectors: dict[str, np.ndarray] = {}
        daily: dict[str, pd.DataFrame] = {}
        self._beta = {}
        for sym, inst in ALL_INSTRUMENTS.items():
            if inst.sector not in sectors:
                srng = np.random.default_rng(_seed(self.seed, "sector", inst.sector))
                sectors[inst.sector] = srng.normal(0, 0.006, n)
            rng = np.random.default_rng(_seed(self.seed, sym))
            beta = rng.uniform(0.7, 1.35)
            self._beta[sym] = beta
            idio_vol = rng.uniform(0.009, 0.02)
            drift = rng.normal(0.0001, 0.0003)
            idio = rng.normal(0, idio_vol, n)
            # 평균회귀가 일어나도록 개별 요인에 약한 음의 자기상관을 줍니다.
            idio[1:] = idio[1:] - 0.12 * idio[:-1]
            r = drift + beta * market + sectors[inst.sector] + idio
            close = inst.base_price * np.exp(np.cumsum(r) - np.cumsum(r)[-1])  # 마지막 값이 base_price 근처
            gap = rng.normal(0, idio_vol / 3, n)
            open_ = np.concatenate([[close[0]], close[:-1]]) * np.exp(gap)
            span = np.abs(rng.normal(0, idio_vol, n)) * close
            high = np.maximum(open_, close) + span * 0.6
            low = np.minimum(open_, close) - span * 0.6
            base_vol = rng.uniform(2e5, 6e6) * (100_000 / max(inst.base_price, 1)) ** 0.5
            volume = base_vol * np.exp(rng.normal(0, 0.35, n)) * (1 + 18 * np.abs(r))
            daily[sym] = pd.DataFrame(
                {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
            )
        bench = 2600 * np.exp(np.cumsum(market) - np.cumsum(market)[-1])
        daily[BENCHMARK] = pd.DataFrame({"close": bench, "open": bench, "high": bench, "low": bench, "volume": 0.0}, index=idx)
        self._daily = daily
        vrng = np.random.default_rng(_seed(self.seed, "today", today))
        self._intraday = {
            sym: {
                "open": float(df["close"].iloc[-1]),
                "price": float(df["close"].iloc[-1]),
                "high": float(df["close"].iloc[-1]),
                "low": float(df["close"].iloc[-1]),
                # 시작 시점에 이미 하루 거래의 일부가 진행된 것으로 둡니다.
                "volume": float(df["volume"].iloc[-20:].mean()) * float(vrng.uniform(0.4, 0.9)),
                "prev_close": float(df["close"].iloc[-1]),
            }
            for sym, df in daily.items()
        }
        self._built_for = today

    def _ensure(self, today: date) -> None:
        if self._built_for != today:
            self._build(today)

    # ---- MarketData -------------------------------------------------------
    def tick(self, now: datetime) -> None:
        with self._lock:
            self._ensure(now.date())
            if not self.status(now).open:
                return
            elapsed = 60.0 if self._last_tick is None else max(1.0, (now - self._last_tick).total_seconds())
            self._last_tick = now
            scale = math.sqrt(min(elapsed, 600) / (390 * 60))  # 하루 390분 중 경과 비율
            m = self._rng.normal(0, 0.0095) * scale
            for sym, st in self._intraday.items():
                if sym == BENCHMARK:
                    st["price"] *= math.exp(m)
                    continue
                step = self._beta[sym] * m + self._rng.normal(0, 0.016) * scale
                st["price"] = max(1.0, st["price"] * math.exp(step))
                st["high"] = max(st["high"], st["price"])
                st["low"] = min(st["low"], st["price"])
                base_vol = float(self._daily[sym]["volume"].iloc[-20:].mean())
                st["volume"] += base_vol * (min(elapsed, 600) / (390 * 60)) * (1 + 30 * abs(step))

    def _today_row(self, sym: str, now: datetime) -> pd.DataFrame:
        st = self._intraday[sym]
        ts = pd.Timestamp(now.date())
        return pd.DataFrame(
            {"open": st["open"], "high": st["high"], "low": st["low"], "close": st["price"], "volume": st["volume"]},
            index=pd.DatetimeIndex([ts]),
        )

    def history(self, symbol: str, days: int = 800) -> pd.DataFrame:
        now = now_kst()
        with self._lock:
            self._ensure(now.date())
            sym = symbol if symbol in self._daily else None
            if sym is None:
                raise KeyError(symbol)
            df = self._daily[sym]
            if self.status(now).open or self._last_tick is not None:
                df = pd.concat([df, self._today_row(sym, now)])
            return df.iloc[-days:].copy()

    def quote(self, symbol: str) -> Quote:
        now = now_kst()
        with self._lock:
            self._ensure(now.date())
            st = self._intraday[symbol]
            return Quote(symbol, round(st["price"], 0), round(st["prev_close"], 0), st["volume"], now)

    def benchmark_history(self, days: int = 800) -> pd.Series:
        return self.history(BENCHMARK, days)["close"]

    def benchmark_level(self) -> float:
        now = now_kst()
        with self._lock:
            self._ensure(now.date())
            return float(self._intraday[BENCHMARK]["price"])

    def risky_symbols(self) -> set[str]:
        return set()

    def status(self, now: datetime) -> MarketStatus:
        if self.always_open:
            return MarketStatus(True, f"모의 시세 · 상시 운영 · {now.strftime('%H:%M')}")
        return krx_session_status(now)
