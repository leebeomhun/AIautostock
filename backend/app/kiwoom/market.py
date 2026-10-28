from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.db import now_kst
from app.kiwoom.client import KiwoomClient, to_float, to_int
from app.market.base import MarketStatus, Quote, krx_session_status

CHART_PATH = "/api/dostk/chart"
STKINFO_PATH = "/api/dostk/stkinfo"
KOSPI_INDEX = "001"
ROWS_PER_PAGE = 600  # ka10081 한 페이지에 오는 대략의 일수


class KiwoomMarket:
    """키움 REST 시세: 일봉(ka10081), 현재가(ka10001), 업종 일봉(ka20006)."""

    name = "kiwoom"

    def __init__(
        self,
        client: KiwoomClient,
        cache_dir: Path,
        exchange: str = "KRX",
        quote_ttl: float = 20.0,
    ) -> None:
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = exchange
        self.quote_ttl = quote_ttl
        self._lock = threading.Lock()
        self._quotes: dict[str, tuple[float, Quote]] = {}
        self._hist: dict[str, tuple[str, pd.DataFrame]] = {}
        self._requested: dict[str, int] = {}
        self._bench: tuple[float, pd.Series] | None = None
        self._risky: tuple[str, set[str]] | None = None

    def _code(self, symbol: str) -> str:
        suffix = {"NXT": "_NX", "SOR": "_AL"}.get(self.exchange, "")
        return f"{symbol}{suffix}"

    # ---- 일봉 ---------------------------------------------------------------
    @staticmethod
    def _frame(rows: list[dict], price_scale: float = 1.0) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(
            {
                "open": [abs(to_int(r.get("open_pric"))) / price_scale for r in rows],
                "high": [abs(to_int(r.get("high_pric"))) / price_scale for r in rows],
                "low": [abs(to_int(r.get("low_pric"))) / price_scale for r in rows],
                "close": [abs(to_int(r.get("cur_prc"))) / price_scale for r in rows],
                "volume": [abs(to_int(r.get("trde_qty"))) for r in rows],
            },
            index=pd.to_datetime([str(r.get("dt")) for r in rows], format="%Y%m%d"),
        )
        df = df[~df.index.duplicated(keep="first")].sort_index()
        return df[df["close"] > 0]

    def _cache_file(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}.json"

    def _load_cached(self, symbol: str, today: str, days: int) -> pd.DataFrame | None:
        mem = self._hist.get(symbol)
        if mem and mem[0] == today and self._requested.get(symbol, 0) >= days:
            return mem[1]
        path = self._cache_file(symbol)
        try:
            p = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if p.get("as_of") != today or p.get("requested", 0) < days:
            return None
        df = pd.DataFrame({k: p[k] for k in ("open", "high", "low", "close", "volume")}, index=pd.to_datetime(p["dates"]))
        self._hist[symbol] = (today, df)
        self._requested[symbol] = p["requested"]
        return df

    def _store(self, symbol: str, today: str, df: pd.DataFrame, days: int) -> None:
        self._hist[symbol] = (today, df)
        self._requested[symbol] = days
        payload = {
            "as_of": today,
            "requested": days,
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            **{k: df[k].tolist() for k in df.columns},
        }
        tmp = self._cache_file(symbol).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self._cache_file(symbol))

    def history(self, symbol: str, days: int = 800) -> pd.DataFrame:
        today = now_kst().strftime("%Y-%m-%d")
        df = self._load_cached(symbol, today, days)
        if df is None:
            rows = self.client.paginate(
                "ka10081",
                CHART_PATH,
                {"stk_cd": self._code(symbol), "base_dt": today.replace("-", ""), "upd_stkpc_tp": "1"},
                "stk_dt_pole_chart_qry",
                max_pages=max(1, -(-days // ROWS_PER_PAGE)),
            )
            df = self._frame(rows)
            self._store(symbol, today, df, days)
        df = df.copy()
        cached = self._quotes.get(symbol)
        if cached and krx_session_status(now_kst()).open:
            q = cached[1]
            ts = pd.Timestamp(now_kst().date())
            prev = df.loc[ts] if ts in df.index else None
            df.loc[ts] = {
                "open": float(prev["open"]) if prev is not None else q.price,
                "high": max(float(prev["high"]), q.price) if prev is not None else q.price,
                "low": min(float(prev["low"]), q.price) if prev is not None else q.price,
                "close": q.price,
                "volume": q.volume,
            }
            df = df.sort_index()
        return df.iloc[-days:]

    # ---- 현재가 ---------------------------------------------------------------
    def quote(self, symbol: str) -> Quote:
        now = time.monotonic()
        with self._lock:
            cached = self._quotes.get(symbol)
            if cached and now - cached[0] < self.quote_ttl:
                return cached[1]
        data = self.client.request("ka10001", STKINFO_PATH, {"stk_cd": self._code(symbol)}).data
        price = abs(to_int(data.get("cur_prc")))
        base = abs(to_int(data.get("base_pric"))) or price - to_int(data.get("pred_pre"))
        q = Quote(
            symbol=symbol,
            price=float(price),
            prev_close=float(base),
            volume=float(abs(to_int(data.get("trde_qty")))),
            at=now_kst(),
            per=to_float(data.get("per")) or None,
            pbr=to_float(data.get("pbr")) or None,
        )
        with self._lock:
            self._quotes[symbol] = (now, q)
        return q

    # ---- 지수 (KOSPI 종합) --------------------------------------------------------
    def benchmark_history(self, days: int = 800) -> pd.Series:
        now = time.monotonic()
        if self._bench and now - self._bench[0] < 120:
            return self._bench[1].iloc[-days:]
        today = now_kst().strftime("%Y%m%d")
        rows = self.client.paginate(
            "ka20006",
            CHART_PATH,
            {"inds_cd": KOSPI_INDEX, "base_dt": today},
            "inds_dt_pole_qry",
            max_pages=max(1, -(-days // ROWS_PER_PAGE)),
        )
        # 지수 값은 소수점을 없앤 100배 값으로 옵니다.
        series = self._frame(rows, price_scale=100.0)["close"]
        self._bench = (now, series)
        return series.iloc[-days:]

    def benchmark_level(self) -> float:
        series = self.benchmark_history(30)
        return float(series.iloc[-1]) if len(series) else 0.0

    def risky_symbols(self) -> set[str]:
        """관리종목·거래정지·투자유의 종목 (ka10099 종목정보 리스트, 하루 한 번 조회)."""
        today = now_kst().strftime("%Y-%m-%d")
        if self._risky and self._risky[0] == today:
            return self._risky[1]
        risky: set[str] = set()
        for market_type in ("0", "10"):  # 코스피, 코스닥
            for r in self.client.paginate("ka10099", STKINFO_PATH, {"mrkt_tp": market_type}, "list", max_pages=10):
                warning = str(r.get("orderWarning") or "0").strip()
                state = str(r.get("state") or "")
                audit = str(r.get("auditInfo") or "")
                if warning not in ("", "0") or any(k in state for k in ("관리", "정지")) or any(k in audit for k in ("주의", "경고", "위험")):
                    risky.add(str(r.get("code") or "").strip())
        self._risky = (today, risky)
        return risky

    def status(self, now: datetime) -> MarketStatus:
        return krx_session_status(now)

    def tick(self, now: datetime) -> None:
        # 캐시는 TTL 로 관리하므로 할 일이 없습니다.
        return None
