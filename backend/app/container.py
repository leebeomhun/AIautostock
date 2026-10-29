from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.ai.analyst import AIAnalyst
from app.insights import Insights
from app.config import Settings
from app.db import make_session_factory
from app.market.base import MarketData
from app.market.calendar import set_extra_holidays
from app.market.simulated import SimulatedMarket
from app.trading.broker import Broker, PaperBroker
from app.trading.engine import TradingEngine

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    session_factory: sessionmaker[Session]
    market: MarketData
    broker: Broker
    analyst: AIAnalyst
    engine: TradingEngine
    insights: Insights
    backtest_cache: dict = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start_loop(self) -> None:
        if self._thread or not self.settings.engine_enabled:
            return

        def run() -> None:
            while not self._stop.is_set():
                self.engine.tick()
                self._stop.wait(self.settings.engine_interval_seconds)

        self._thread = threading.Thread(target=run, name="trading-engine", daemon=True)
        self._thread.start()
        log.info("매매 엔진 시작 (주기 %ss)", self.settings.engine_interval_seconds)

    def stop_loop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


def build_market(settings: Settings) -> tuple[MarketData, object | None]:
    """시세만 필요한 배치 작업(ML·리서치)도 쓰도록 시세 객체를 따로 만듭니다. (시세, 키움 클라이언트 또는 None)"""
    set_extra_holidays(settings.krx_extra_holidays)
    if settings.use_kiwoom:
        from app.kiwoom.client import KiwoomClient
        from app.kiwoom.market import KiwoomMarket

        app_key, app_secret = settings.kiwoom_credentials
        client = KiwoomClient(
            app_key or "",
            app_secret or "",
            mode=settings.kiwoom_mode,
            max_rps=settings.kiwoom_max_rps,
            token_cache=settings.cache_dir / f"token-{settings.kiwoom_mode}.json",
        )
        return KiwoomMarket(client, settings.cache_dir / settings.kiwoom_mode, exchange=settings.kiwoom_exchange), client
    return SimulatedMarket(seed=settings.sim_seed, always_open=settings.sim_always_open), None


def build_container(settings: Settings) -> Container:
    session_factory = make_session_factory(settings.database_url)
    market: MarketData
    broker: Broker
    market, client = build_market(settings)
    if client is not None:
        from app.kiwoom.broker import KiwoomBroker

        broker = KiwoomBroker(client, exchange=settings.kiwoom_exchange)
        log.info("키움 REST 연결: %s (%s)", client.base_url, broker.label)
    else:
        broker = PaperBroker(
            session_factory,
            market,
            initial_cash=settings.sim_initial_cash,
            fee_rate=settings.fee_rate,
            sell_tax_rate=settings.sell_tax_rate,
            slippage_rate=settings.slippage_rate,
        )
        log.info("키움 키가 없어 로컬 시뮬레이션으로 실행합니다")
    analyst = AIAnalyst(
        settings.openai_api_key,
        settings.openai_model,
        settings.openai_reasoning_effort,
        settings.openai_news_search,
    )
    insights = Insights(settings.data_dir)
    engine = TradingEngine(settings, session_factory, market, broker, analyst, insights)
    return Container(settings, session_factory, market, broker, analyst, engine, insights)
