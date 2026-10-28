"""해시 체인 판단 원장.

ai-hedge-fund 의 hash-chained ledger 개념을 참고해 직접 구현했습니다. 판단 생성·승인·거절·만료·주문·체결·모드 변경·긴급 정지를
순서대로 기록하고, 각 항목의 해시에 직전 항목의 해시를 넣어 중간 기록을 고치거나 지우면 검증에서 드러나게 합니다.
기록은 엔진 잠금 안에서만 추가하므로 순서가 엇갈리지 않습니다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import iso_kst
from app.models import LedgerEntry

GENESIS = "0" * 64


def _canonical(seq: int, at: datetime, kind: str, ref: str | None, payload: dict[str, Any], prev: str) -> bytes:
    body = {"seq": seq, "at": at.isoformat(timespec="microseconds"), "kind": kind, "ref": ref, "payload": payload, "prev": prev}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def append(s: Session, kind: str, ref: str | None, payload: dict[str, Any], at: datetime) -> LedgerEntry:
    s.flush()
    last = s.scalar(select(LedgerEntry).order_by(LedgerEntry.seq.desc()).limit(1))
    seq = (last.seq + 1) if last else 1
    prev = last.hash if last else GENESIS
    # JSON 왕복으로 저장 형태와 해시 입력을 똑같이 맞춥니다.
    clean = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    digest = hashlib.sha256(_canonical(seq, at, kind, ref, clean, prev)).hexdigest()
    entry = LedgerEntry(seq=seq, at=at, kind=kind, ref=ref, payload=clean, prev_hash=prev, hash=digest)
    s.add(entry)
    s.flush()
    return entry


def verify(s: Session) -> dict[str, Any]:
    prev = GENESIS
    count = 0
    for e in s.scalars(select(LedgerEntry).order_by(LedgerEntry.seq)).yield_per(500):
        count += 1
        expected = hashlib.sha256(_canonical(e.seq, e.at, e.kind, e.ref, e.payload, prev)).hexdigest()
        if e.prev_hash != prev or e.hash != expected or e.seq != count:
            return {"ok": False, "count": count, "broken_at": e.seq, "head": prev}
        prev = e.hash
    return {"ok": True, "count": count, "broken_at": None, "head": prev}


def entries_for(s: Session, ref: str) -> list[dict[str, Any]]:
    rows = s.scalars(select(LedgerEntry).where(LedgerEntry.ref == ref).order_by(LedgerEntry.seq)).all()
    return [
        {"seq": e.seq, "at": iso_kst(e.at), "kind": e.kind, "payload": e.payload, "prev_hash": e.prev_hash, "hash": e.hash}
        for e in rows
    ]


def decision_payload(d: Any) -> dict[str, Any]:
    """판단을 만들 때 원장에 남기는 스냅샷. AI 가 본 근거와 결론을 그대로 남깁니다."""
    return {
        "symbol": d.symbol,
        "side": d.side,
        "reason": d.reason,
        "qty": d.qty,
        "price": d.price,
        "confidence": d.confidence,
        "summary": d.summary,
        "signals": d.signals,
        "risk_rules": d.risk_rules,
        "model": d.model,
        "mode": d.mode,
        "status": d.status,
        "strategy": d.strategy_label,
        "data_at": d.data_at,
    }
