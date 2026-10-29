"""기관·외국인 연속 순매수(키움 ka10131) 일별 스냅샷.

엔진의 수급 가산(+0.03, signals.FLOW_BONUS)은 과거 수급 데이터가 없어 백테스트로 검증하지 못했습니다.
나중에 검증할 수 있도록 하루 1–2회 data/flow/<날짜>.json 에 남깁니다.
- close_window: 엔진이 장 마감 판단 구간에 모멘텀 후보 점수를 매기며 받은 목록 (그날 처음 받았을 때 한 번)
- eod: 18:40 야간 배치(app.ml.nightly)가 장 마감 뒤 받은 목록
형식: {"date": ..., "snapshots": {label: {"taken_at": ..., "rows": {종목코드: [기관 연속일수, 외국인 연속일수]}}}}. 음수는 연속 순매도.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def flow_path(data_dir: Path, day: str) -> Path:
    return Path(data_dir) / "flow" / f"{day}.json"


def save_snapshot(data_dir: Path, flow: dict[str, tuple[int, int]], now: datetime, label: str) -> Path:
    """같은 날 같은 label 은 덮어씁니다. 다른 label 은 그대로 둡니다."""
    day = now.strftime("%Y-%m-%d")
    path = flow_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_snapshots(data_dir, day) or {"date": day, "snapshots": {}}
    data["snapshots"][label] = {"taken_at": now.isoformat(timespec="seconds"), "rows": {code: list(v) for code, v in sorted(flow.items())}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)
    return path


def load_snapshots(data_dir: Path, day: str) -> dict[str, Any] | None:
    try:
        return json.loads(flow_path(data_dir, day).read_text())
    except (OSError, ValueError):
        return None
