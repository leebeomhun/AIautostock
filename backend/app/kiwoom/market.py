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
# 오늘 봉이 확정됐다고 보는 시각. 15:30 장 마감 뒤 종가가 일봉 차트에 반영될 여유를 둡니다.
BAR_SETTLED_MINUTES = 15 * 60 + 40


class KiwoomMarket:
    """키움 REST 시세: 일봉(ka10081), 현재가(ka10001), 업종 일봉(ka20006)."""

    name = "kiwoom"

    def __init__(
        self,
        client: KiwoomClient,
        cache_dir: Path,
        exchange: str = "KRX",
        quote_ttl: float = 20.0,
        intraday_ttl: float = 300.0,
    ) -> None:
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = exchange
        self.quote_ttl = quote_ttl
        # 장중에는 일봉 캐시를 이 간격(초)마다 최근 한 페이지만 다시 받아 오늘 봉의 고가·저가·종가·거래량을 맞춥니다.
        self.intraday_ttl = intraday_ttl
        self._lock = threading.Lock()
        self._quotes: dict[str, tuple[float, Quote]] = {}
        self._hist: dict[str, tuple[datetime, int, pd.DataFrame]] = {}  # 받은 시각, 요청 일수, 일봉
        self._bench: tuple[float, pd.Series] | None = None
        self._risky: tuple[str, set[str]] | None = None
        self._lists: dict[str, tuple[str, list[dict]]] = {}

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

    def _load_cached(self, symbol: str) -> tuple[datetime, int, pd.DataFrame] | None:
        mem = self._hist.get(symbol)
        if mem:
            return mem
        try:
            p = json.loads(self._cache_file(symbol).read_text())
        except (OSError, ValueError):
            return None
        # 받은 시각이 없는 예전 캐시는 그날 0시에 받은 것으로 봐서 장중·장 마감 뒤에는 다시 받습니다.
        fetched = datetime.fromisoformat(p.get("fetched_at") or p["as_of"])
        df = pd.DataFrame({k: p[k] for k in ("open", "high", "low", "close", "volume")}, index=pd.to_datetime(p["dates"]))
        self._hist[symbol] = (fetched, p.get("requested", 0), df)
        return self._hist[symbol]

    def _store(self, symbol: str, fetched: datetime, df: pd.DataFrame, days: int) -> None:
        self._hist[symbol] = (fetched, days, df)
        payload = {
            "as_of": fetched.strftime("%Y-%m-%d"),
            "fetched_at": fetched.isoformat(timespec="seconds"),
            "requested": days,
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            **{k: df[k].tolist() for k in df.columns},
        }
        tmp = self._cache_file(symbol).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self._cache_file(symbol))

    @staticmethod
    def _bar_phase(at: datetime) -> str:
        """오늘 봉의 상태: before(아직 없음) · live(바뀌는 중) · settled(확정). 주말은 오늘 봉이 없어 settled 로 봅니다."""
        if at.weekday() >= 5:
            return "settled"
        minutes = at.hour * 60 + at.minute
        if minutes < 9 * 60:
            return "before"
        return "live" if minutes < BAR_SETTLED_MINUTES else "settled"

    def _bar_fresh(self, fetched: datetime, now: datetime) -> bool:
        """같은 날 받은 일봉 캐시를 그대로 써도 되는지.

        과거 봉은 바뀌지 않지만 오늘 봉은 장중 내내 바뀝니다. 그래서 장중에는 intraday_ttl 이 지나면,
        장 마감 뒤에는 장중에 받은 캐시를 한 번 더 받아 확정 봉으로 바꿉니다. 장 시작 전과 확정 뒤에는 그대로 씁니다.
        """
        if fetched.date() != now.date():
            return False
        phase, fetched_phase = self._bar_phase(now), self._bar_phase(fetched)
        if phase == "live":
            return (now - fetched).total_seconds() < self.intraday_ttl
        return phase == fetched_phase

    def history(self, symbol: str, days: int = 800) -> pd.DataFrame:
        now = now_kst()
        cached = self._load_cached(symbol)
        if cached and cached[1] >= days and self._bar_fresh(cached[0], now):
            fetched, df = cached[0], cached[2]
        else:
            # 같은 날 받은 캐시가 있으면 최근 한 페이지만 다시 받아 오늘 봉(과 최근 봉)만 바꿉니다.
            partial = cached is not None and cached[1] >= days and cached[0].date() == now.date()
            rows = self.client.paginate(
                "ka10081",
                CHART_PATH,
                {"stk_cd": self._code(symbol), "base_dt": now.strftime("%Y%m%d"), "upd_stkpc_tp": "1"},
                "stk_dt_pole_chart_qry",
                max_pages=1 if partial else max(1, -(-days // ROWS_PER_PAGE)),
            )
            fresh = self._frame(rows)
            if partial:
                old = cached[2]
                df = pd.concat([old[old.index < fresh.index.min()], fresh]) if len(fresh) else old
                days = cached[1]
            else:
                df = fresh
            fetched = now
            self._store(symbol, fetched, df, days)
        df = df.copy()
        cached_quote = self._quotes.get(symbol)
        # 일봉을 받은 뒤에 조회한 현재가가 있으면 오늘 봉에 덧씌워 가장 최근 값으로 맞춥니다.
        if cached_quote and krx_session_status(now).open and cached_quote[1].at > fetched:
            q = cached_quote[1]
            ts = pd.Timestamp(now.date())
            prev = df.loc[ts] if ts in df.index else None
            high = max(q.price, q.extra.get("high", 0.0))
            low = min(q.price, q.extra.get("low") or q.price)
            df.loc[ts] = {
                "open": float(prev["open"]) if prev is not None else q.extra.get("open") or q.price,
                "high": max(float(prev["high"]), high) if prev is not None else high,
                "low": min(float(prev["low"]), low) if prev is not None else low,
                "close": q.price,
                "volume": max(q.volume, float(prev["volume"])) if prev is not None else q.volume,
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
            # 일봉에 덧씌울 오늘 시가·고가·저가 (값이 없으면 넣지 않습니다)
            extra={k: float(v) for k, v in (("open", abs(to_int(data.get("open_pric")))), ("high", abs(to_int(data.get("high_pric")))), ("low", abs(to_int(data.get("low_pric"))))) if v},
        )
        with self._lock:
            self._quotes[symbol] = (now, q)
        return q

    def stock_info(self, symbol: str) -> dict[str, float | str]:
        """주식기본정보(ka10001)의 펀더멘털 항목. 심층 리서치의 펀더멘털 분석가가 읽습니다."""
        data = self.client.request("ka10001", STKINFO_PATH, {"stk_cd": self._code(symbol)}).data
        num = {
            "per": "PER", "pbr": "PBR", "roe": "ROE(%)", "eps": "EPS(원)", "bps": "BPS(원)",
            "mac": "시가총액(억원)", "sale_amt": "매출액(억원)", "bus_pro": "영업이익(억원)", "cup_nga": "당기순이익(억원)",
            "for_exh_rt": "외인소진률(%)", "250hgst": "250일 최고(원)", "250lwst": "250일 최저(원)", "cur_prc": "현재가(원)", "flu_rt": "등락률(%)",
        }
        out: dict[str, float | str] = {"종목명": str(data.get("stk_nm") or symbol), "결산월": str(data.get("setl_mm") or "")}
        for key, label in num.items():
            raw = data.get(key)
            if raw not in (None, ""):
                out[label] = abs(to_float(raw)) if key in ("cur_prc", "250hgst", "250lwst") else to_float(raw)
        return out

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

    def _stock_list(self, market_type: str) -> list[dict]:
        """ka10099 종목정보 리스트 (시장별 하루 한 번 조회)."""
        today = now_kst().strftime("%Y-%m-%d")
        cached = self._lists.get(market_type)
        if cached and cached[0] == today:
            return cached[1]
        rows = self.client.paginate("ka10099", STKINFO_PATH, {"mrkt_tp": market_type}, "list", max_pages=10)
        self._lists[market_type] = (today, rows)
        return rows

    def top_by_market_cap(self, limit: int, market_type: str = "0") -> list[str]:
        """보통주 시가총액(상장주식수 × 현재가) 상위 종목. ETF·ETN·리츠·우선주·스팩과 관리·정지 종목은 뺍니다."""
        caps: list[tuple[int, str]] = []
        for r in self._stock_list(market_type):
            code = str(r.get("code") or "").strip()
            name = str(r.get("name") or "")
            state = str(r.get("state") or "")
            if r.get("marketName") not in ("거래소", "코스닥") or len(code) != 6 or not code.endswith("0"):
                continue
            if "스팩" in name or any(k in state for k in ("관리", "정지")):
                continue
            caps.append((to_int(r.get("listCount")) * to_int(r.get("lastPrice")), code))
        caps.sort(reverse=True)
        return [code for _cap, code in caps[:limit]]

    def risky_symbols(self) -> set[str]:
        """관리종목·거래정지·투자유의 종목 (ka10099 종목정보 리스트, 하루 한 번 조회)."""
        today = now_kst().strftime("%Y-%m-%d")
        if self._risky and self._risky[0] == today:
            return self._risky[1]
        risky: set[str] = set()
        for market_type in ("0", "10"):  # 코스피, 코스닥
            for r in self._stock_list(market_type):
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
