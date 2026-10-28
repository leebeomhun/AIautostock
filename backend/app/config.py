from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    """환경 변수와 .env(프로젝트 루트 또는 backend/)에서 읽는 서버 설정."""

    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "AutoStock"
    # 공개 도메인에 올릴 때 필수: 설정하면 /api 전체에 로그인을 요구합니다.
    app_password: str | None = None
    session_secret: str | None = None
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'autostock.db'}"
    cache_dir: Path = BACKEND_DIR / "data" / "cache"
    frontend_dist: Path = ROOT_DIR / "frontend" / "dist"

    # OpenAI — 판단 근거 생성과 최종 판단
    openai_api_key: str | None = None
    openai_model: str = "gpt-6.1-sol"
    openai_reasoning_effort: Literal["low", "medium", "high"] = "low"
    openai_news_search: bool = True
    openai_max_calls_per_tick: int = 3

    # 키움증권 REST API — 모의투자와 실전은 앱키가 다릅니다. KIWOOM_MODE 에 맞는 쌍을 씁니다.
    kiwoom_mode: Literal["mock", "real"] = "mock"
    kiwoom_app_key: str | None = Field(default=None, validation_alias=AliasChoices("KIWOOM_APP_KEY", "KIWOOM_APPKEY"))
    kiwoom_app_secret: str | None = Field(
        default=None, validation_alias=AliasChoices("KIWOOM_APP_SECRET", "KIWOOM_SECRET_KEY", "KIWOOM_SECRETKEY")
    )
    kiwoom_mock_app_key: str | None = Field(default=None, validation_alias=AliasChoices("KIWOOM_MOCK_APP_KEY", "KIWOOM_MOCK_APPKEY"))
    kiwoom_mock_app_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("KIWOOM_MOCK_APP_SECRET", "KIWOOM_MOCK_SECRET_KEY", "KIWOOM_MOCK_SECRETKEY"),
    )
    kiwoom_max_rps: float = 4.0
    kiwoom_exchange: Literal["KRX", "NXT", "SOR"] = "KRX"

    # 시세/브로커 선택: auto = 키움 키가 있으면 키움, 없으면 로컬 시뮬레이션
    data_source: Literal["auto", "kiwoom", "simulated"] = "auto"

    # 엔진
    engine_enabled: bool = True
    engine_interval_seconds: int = 60
    approval_timeout_seconds: int = 300
    sim_always_open: bool = True
    sim_initial_cash: int = 20_000_000
    sim_seed: int = 7

    # 비용 가정 — 백테스트와 로컬 시뮬레이션에만 쓰입니다. 실제 수수료·세율은 계좌와 시점에 맞게 조정하세요.
    fee_rate: float = 0.00015
    sell_tax_rate: float = 0.0018
    slippage_rate: float = 0.0005

    # 실전 계좌(KIWOOM_MODE=real)에서 주문을 허용하기 전 필요한 모의투자 운영 일수
    paper_days_before_live: int = 14

    # 보호 규칙 (app.trading.protections)
    protect_stoploss_cooldown_days: int = 3
    protect_exit_cooldown_days: int = 1
    protect_stoploss_guard_count: int = 3
    protect_max_drawdown_pct: float = 10.0

    # 배치 결과(ML 점수·심층 리서치)를 읽는 폴더
    data_dir: Path = BACKEND_DIR / "data"

    # 심층 리서치 (app.research.daily) — 하루 한 번, 종목당 OpenAI 호출 6회 안팎
    research_engine: Literal["builtin", "tradingagents"] = "builtin"
    research_max_symbols: int = 5
    research_quick_model: str = "gpt-6.1-sol"
    research_deep_model: str | None = None

    @property
    def kiwoom_credentials(self) -> tuple[str | None, str | None]:
        if self.kiwoom_mode == "real":
            return self.kiwoom_app_key, self.kiwoom_app_secret
        return self.kiwoom_mock_app_key, self.kiwoom_mock_app_secret

    @property
    def use_kiwoom(self) -> bool:
        if self.data_source == "kiwoom":
            return True
        if self.data_source == "simulated":
            return False
        key, secret = self.kiwoom_credentials
        return bool(key and secret)

    @property
    def is_live_account(self) -> bool:
        return self.use_kiwoom and self.kiwoom_mode == "real"


@lru_cache
def get_settings() -> Settings:
    return Settings()
