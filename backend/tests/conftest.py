from __future__ import annotations

import pytest

from app.config import Settings
from app.container import Container, build_container
from app.db import now_kst, session_scope
from app.models import AccountSettings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_source="simulated",
        openai_api_key=None,
        kiwoom_app_key=None,
        kiwoom_app_secret=None,
        kiwoom_mock_app_key=None,
        kiwoom_mock_app_secret=None,
        engine_enabled=False,
        frontend_dist=tmp_path / "no-dist",
    )


@pytest.fixture
def container(settings) -> Container:
    c = build_container(settings)
    with session_scope(c.session_factory) as s:
        st = s.get(AccountSettings, 1)
        st.onboarded = True
        st.agreed = True
        st.paper_started_at = now_kst()
    return c
