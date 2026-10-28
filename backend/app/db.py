from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """DB에는 한국 시간(naive)으로 저장합니다. KRX만 다루므로 시간대를 하나로 고정합니다."""
    return datetime.now(KST).replace(tzinfo=None)


def iso_kst(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=KST).isoformat()


class Base(DeclarativeBase):
    pass


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    if database_url.startswith("sqlite:///"):
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30} if "sqlite" in database_url else {})

    if "sqlite" in database_url:

        @event.listens_for(engine, "connect")
        def _pragma(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    from app import models  # noqa: F401  (테이블 등록)

    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
