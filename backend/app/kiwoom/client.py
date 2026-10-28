"""키움증권 REST API 클라이언트.

명세: https://github.com/Kiwoom-Securities/Kiwoom-REST-API (kiwoom/_data/kiwoom_api_spec.json)
- 모든 요청은 POST + JSON, `api-id` 헤더로 TR 을 지정합니다.
- 업무 오류도 HTTP 200 으로 오고 성공 여부는 본문 `return_code`(0=정상)에만 담깁니다.
- 연속조회는 응답 헤더 `cont-yn`=Y 일 때 `next-key` 를 다음 요청 헤더에 넣습니다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URLS = {"real": "https://api.kiwoom.com", "mock": "https://mockapi.kiwoom.com"}

TOKEN_INVALID_CODES = {8005}
RATE_LIMIT_CODES = {1700, 1701, 1702}


class KiwoomError(RuntimeError):
    def __init__(self, code: int | None, message: str, api_id: str = "") -> None:
        super().__init__(f"[{api_id}] {message} (return_code={code})")
        self.code = code
        self.message = message
        self.api_id = api_id


@dataclass
class KiwoomResponse:
    data: dict[str, Any]
    cont_yn: str | None
    next_key: str | None


def _return_code(data: dict[str, Any]) -> int | None:
    raw = data.get("return_code")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int:
    """'+70100', '-00000000196888', '000000012300000' 같은 키움 숫자 문자열을 정수로."""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def to_float(value: Any) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "").replace("%", "")
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


class KiwoomClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        mode: str = "mock",
        max_rps: float = 4.0,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        token_cache: Path | None = None,
    ) -> None:
        if not app_key or not app_secret:
            raise ValueError("KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 비어 있습니다")
        self.mode = mode
        self.base_url = BASE_URLS["real" if mode == "real" else "mock"]
        self._app_key = app_key
        self._app_secret = app_secret
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self._token: str | None = None
        self._token_expires: datetime | None = None
        # 서버와 배치 작업이 같은 앱키로 토큰을 번갈아 새로 받지 않도록 파일로 공유합니다.
        self._token_cache = token_cache
        self._token_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0
        self._last_call = 0.0

    # ---- 인증 (au10001) --------------------------------------------------
    def _issue_token(self) -> None:
        resp = self._http.post(
            "/oauth2/token",
            headers={"Content-Type": "application/json;charset=UTF-8", "api-id": "au10001"},
            json={"grant_type": "client_credentials", "appkey": self._app_key, "secretkey": self._app_secret},
        )
        data = self._json(resp, "au10001")
        code = _return_code(data)
        token = data.get("token")
        if (code not in (None, 0)) or not token:
            raise KiwoomError(code, data.get("return_msg") or "접근토큰 발급 실패", "au10001")
        self._token = token
        expires = data.get("expires_dt")
        try:
            self._token_expires = datetime.strptime(str(expires), "%Y%m%d%H%M%S")
        except (TypeError, ValueError):
            self._token_expires = datetime.now() + timedelta(hours=12)
        log.info("키움 접근토큰 발급 (%s, 만료 %s)", self.mode, self._token_expires)
        self._save_token()

    def _load_token(self) -> bool:
        if not self._token_cache:
            return False
        try:
            data = json.loads(self._token_cache.read_text())
            if data.get("base_url") != self.base_url or data.get("key_tail") != self._app_key[-6:]:
                return False
            expires = datetime.fromisoformat(data["expires"])
        except (OSError, ValueError, KeyError):
            return False
        if datetime.now() >= expires - timedelta(minutes=5):
            return False
        self._token, self._token_expires = data["token"], expires
        return True

    def _save_token(self) -> None:
        if not self._token_cache or not self._token or not self._token_expires:
            return
        self._token_cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_cache.with_suffix(".tmp")
        payload = {"base_url": self.base_url, "key_tail": self._app_key[-6:], "token": self._token, "expires": self._token_expires.isoformat()}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        tmp.replace(self._token_cache)

    def _authorization(self, force_refresh: bool = False) -> str:
        with self._token_lock:
            # 만료 5분 전에는 미리 재발급합니다.
            stale = self._token_expires is None or datetime.now() >= self._token_expires - timedelta(minutes=5)
            if force_refresh:
                rejected = self._token
                # 다른 프로세스가 이미 새 토큰을 받아 두었으면 그것을 쓰고, 아니면 새로 받습니다.
                if not self._load_token() or self._token == rejected:
                    self._issue_token()
            elif self._token is None or stale:
                if not self._load_token():
                    self._issue_token()
            return f"Bearer {self._token}"

    # ---- 공통 요청 -------------------------------------------------------
    def _throttle(self) -> None:
        with self._rate_lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    @staticmethod
    def _json(resp: httpx.Response, api_id: str) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise KiwoomError(None, f"JSON 이 아닌 응답 (HTTP {resp.status_code})", api_id) from exc
        if not isinstance(data, dict):
            raise KiwoomError(None, "예상하지 못한 응답 형식", api_id)
        return data

    def request(
        self,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> KiwoomResponse:
        refreshed = False
        for attempt in range(4):
            self._throttle()
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": api_id,
                "authorization": self._authorization(),
            }
            if cont_yn:
                headers["cont-yn"] = cont_yn
            if next_key:
                headers["next-key"] = next_key
            resp = self._http.post(path, headers=headers, json=body)
            if resp.status_code == 401 and not refreshed:
                self._authorization(force_refresh=True)
                refreshed = True
                continue
            if resp.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            data = self._json(resp, api_id)
            code = _return_code(data)
            if code in TOKEN_INVALID_CODES and not refreshed:
                self._authorization(force_refresh=True)
                refreshed = True
                continue
            if code in RATE_LIMIT_CODES:
                time.sleep(1.0 + attempt)
                continue
            if code not in (None, 0):
                raise KiwoomError(code, data.get("return_msg") or "요청 실패", api_id)
            if resp.status_code >= 400:
                raise KiwoomError(None, f"HTTP {resp.status_code}", api_id)
            return KiwoomResponse(
                data=data,
                cont_yn=resp.headers.get("cont-yn"),
                next_key=resp.headers.get("next-key"),
            )
        raise KiwoomError(None, "재시도 횟수 초과 (유량 제한 또는 인증 오류)", api_id)

    def paginate(self, api_id: str, path: str, body: dict[str, Any], list_key: str, max_pages: int = 3) -> list[dict]:
        rows: list[dict] = []
        cont_yn: str | None = None
        next_key: str | None = None
        for _ in range(max_pages):
            resp = self.request(api_id, path, body, cont_yn=cont_yn, next_key=next_key)
            rows.extend(resp.data.get(list_key) or [])
            if resp.cont_yn != "Y" or not resp.next_key:
                break
            cont_yn, next_key = "Y", resp.next_key
        return rows

    def close(self) -> None:
        self._http.close()
