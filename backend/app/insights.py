"""야간·장전 배치가 만든 보조 판단을 읽습니다.

- ML 점수: ml/nightly.py (qlib Alpha158 + LightGBM) 가 data/ml/latest.json 에 종목별 예측 순위를 씁니다.
- 심층 리서치: research/daily.py (TradingAgents) 가 data/research/latest.json 과 리포트(.md)를 씁니다.
엔진은 이 값을 근거 시그널로 붙이고, 리서치 의견이 Underweight·Sell 인 종목은 신규 매수 점수를 깎고 매수 금액을 줄입니다.
파일이 없거나 오래됐으면 조용히 건너뜁니다 (배치가 실패해도 매매는 계속).
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.quant.signals import Signal, clamp

# 약세 의견 종목의 신규 매수: (퀀트 점수 감점, 종목 비중 배수). 막지는 않고 순위를 낮추고 적게 삽니다.
# 리서치는 하루 한 번 장전에 돌아 장중 돌파를 반영하지 못하므로 거부권 대신 감점으로 씁니다.
RESEARCH_BUY_HAIRCUT: dict[str, tuple[float, float]] = {"Underweight": (0.1, 0.5), "Sell": (0.2, 0.25)}
RATING_SIDE = {"Buy": ("buy", 0.8), "Overweight": ("buy", 0.55), "Hold": ("neutral", 0.2), "Underweight": ("sell", 0.55), "Sell": ("sell", 0.8)}
RATING_KO = {"Buy": "매수", "Overweight": "비중 확대", "Hold": "보유", "Underweight": "비중 축소", "Sell": "매도"}
# ML 순위 상위 이 비율 안의 종목은 매수 점수를 더 올립니다 (예측력이 검증된 경우만). 순위 보정 ±0.05 에 더해 최대 +0.1.
ML_TOP_PCT = 0.2
ML_TOP_BONUS = 0.05


class Insights:
    def __init__(self, data_dir: Path, ml_max_age_days: int = 4, research_max_age_days: int = 3, min_rank_ic: float = 0.02) -> None:
        self.min_rank_ic = min_rank_ic
        self.ml_path = Path(data_dir) / "ml" / "latest.json"
        self.research_dir = Path(data_dir) / "research"
        self.ml_max_age_days = ml_max_age_days
        self.research_max_age_days = research_max_age_days
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[float, Any]] = {}

    def _load(self, path: Path) -> Any:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        with self._lock:
            self._cache[path] = (mtime, data)
        return data

    @staticmethod
    def _fresh(as_of: str | None, max_days: int, today: date) -> bool:
        if not as_of:
            return False
        try:
            d = datetime.strptime(as_of[:10], "%Y-%m-%d").date()
        except ValueError:
            return False
        return (today - d).days <= max_days

    # ---- ML ---------------------------------------------------------------
    def ml_status(self) -> dict[str, Any] | None:
        data = self._load(self.ml_path)
        if not data:
            return None
        return {"as_of": data.get("as_of"), "model": data.get("model"), "count": len(data.get("scores", {})), "metrics": data.get("metrics")}

    def ml_for(self, symbol: str, today: date) -> dict[str, Any] | None:
        data = self._load(self.ml_path)
        if not data or not self._fresh(data.get("as_of"), self.ml_max_age_days, today):
            return None
        row = data.get("scores", {}).get(symbol)
        if not row:
            return None
        m = data.get("metrics") or {}
        return row | {
            "as_of": data.get("as_of"),
            "model": data.get("model"),
            "count": len(data["scores"]),
            "valid_rank_ic": m.get("valid_rank_ic"),
            "fold_rank_ics": m.get("fold_rank_ics"),
            "horizon": m.get("label_horizon", 1),
        }

    def ml_signal(self, symbol: str, side: str, today: date) -> tuple[Signal, float] | None:
        """(근거 시그널, 점수 보정값). 상위 30% 는 매수 쪽, 하위 30% 는 관망·매도 쪽 근거.

        매수 점수 보정은 순위에 따라 ±0.05 이고, 상위 ML_TOP_PCT 는 ML_TOP_BONUS 를 더합니다.
        엔진은 이 보정을 진입 후보 정렬 전에 반영합니다. 검증 순위 IC 가 낮거나 불안정하면 보정 없이 참고만 합니다.
        """
        row = self.ml_for(symbol, today)
        if row is None:
            return None
        top = row["pct"]  # 0 = 1위, 1 = 꼴찌
        horizon = "다음 거래일" if row["horizon"] == 1 else f"향후 {row['horizon']}거래일"
        detail = f"{row['count']}종목 중 {row['rank']}위 (상위 {top * 100:.0f}%) · {horizon} 예측 순위 · {row['as_of']} 기준"
        if side == "buy":
            sig_side = "buy" if top <= 0.3 else "hold" if top >= 0.7 else "neutral"
        else:
            sig_side = "sell" if top >= 0.7 else "hold" if top <= 0.3 else "neutral"
        strength = clamp(abs(0.5 - top) * 2)
        adjust = (0.5 - top) * 0.1 + (ML_TOP_BONUS if top <= ML_TOP_PCT else 0.0) if side == "buy" else (top - 0.5) * 0.1
        ic = row.get("valid_rank_ic")
        folds = row.get("fold_rank_ics") or []
        positive = sum(1 for v in folds if v > 0)
        low = ic is None or ic != ic or ic < self.min_rank_ic
        # 검증 구간 과반에서 순위 IC 가 양수여야 합니다. 한 구간만 좋았던 모델은 쓰지 않습니다.
        unstable = bool(folds) and positive * 2 <= len(folds)
        if low or unstable:
            # 최근 검증 구간에서 예측력이 없으면 근거로만 보여 주고 판단에는 반영하지 않습니다.
            ic_text = "없음" if ic is None or ic != ic else f"{ic:.3f}"
            why = "예측력이 낮아" if low else f"검증 구간 {len(folds)}개 중 {positive}개만 양수라"
            return Signal("ml", "ML 랭킹 (qlib)", f"{detail} · 검증 순위 IC {ic_text}, {why} 참고만", "neutral", 0.0), 0.0
        return Signal("ml", "ML 랭킹 (qlib)", f"{detail} · 검증 순위 IC {ic:.3f}", sig_side, strength), adjust

    # ---- 심층 리서치 ----------------------------------------------------------
    def research_status(self) -> dict[str, Any] | None:
        data = self._load(self.research_dir / "latest.json")
        if not data:
            return None
        return {"as_of": data.get("as_of"), "model": data.get("model"), "count": len(data.get("items", {}))}

    def research_for(self, symbol: str, today: date) -> dict[str, Any] | None:
        data = self._load(self.research_dir / "latest.json")
        if not data:
            return None
        row = data.get("items", {}).get(symbol)
        if not row or not self._fresh(row.get("as_of"), self.research_max_age_days, today):
            return None
        return row

    def research_signal(self, symbol: str, today: date) -> Signal | None:
        row = self.research_for(symbol, today)
        if row is None or row.get("rating") not in RATING_SIDE:
            return None
        side, strength = RATING_SIDE[row["rating"]]
        summary = (row.get("summary") or "").strip()
        detail = f"{RATING_KO[row['rating']]}({row['rating']}) · {row['as_of']} 분석" + (f" · {summary}" if summary else "")
        return Signal("research", "심층 리서치 (TradingAgents)", detail[:240], side, strength)

    def research_buy_haircut(self, symbol: str, today: date) -> tuple[float, float, str] | None:
        """약세 의견 종목을 새로 살 때의 (점수 감점, 종목 비중 배수, 안내 문구). 그 밖의 의견이면 None."""
        row = self.research_for(symbol, today)
        rating = row.get("rating") if row else None
        if rating not in RESEARCH_BUY_HAIRCUT:
            return None
        penalty, scale = RESEARCH_BUY_HAIRCUT[rating]
        note = f"심층 리서치 의견 {RATING_KO[rating]}({rating}, {row.get('as_of')}) · 점수 −{penalty:g}, 매수 금액 {scale * 100:.0f}%로 축소"
        return penalty, scale, note

    def research_report(self, symbol: str) -> dict[str, Any] | None:
        data = self._load(self.research_dir / "latest.json")
        row = (data or {}).get("items", {}).get(symbol)
        if not row:
            return None
        path = (self.research_dir / row.get("report", "")).resolve()
        if self.research_dir.resolve() not in path.parents or not path.is_file():
            return row | {"report_text": None}
        return row | {"report_text": path.read_text()}
