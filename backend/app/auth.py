"""단일 사용자 비밀번호 로그인.

APP_PASSWORD 가 있으면 /api 전체(로그인·상태 확인·헬스체크 제외)에 서명된 세션 쿠키를 요구합니다.
매매를 승인·정지할 수 있는 화면이므로 공개 도메인에 올릴 때는 반드시 설정해야 합니다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import Settings

COOKIE = "autostock_session"
SESSION_DAYS = 30
OPEN_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/status", "/api/health"}
MAX_FAILURES = 5
LOCK_SECONDS = 600


def _secret(settings: Settings) -> bytes:
    base = settings.session_secret or f"autostock:{settings.app_password}"
    return hashlib.sha256(base.encode()).digest()


def issue_token(settings: Settings) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + SESSION_DAYS * 86400}).encode()).decode()
    sig = hmac.new(_secret(settings), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_token(settings: Settings, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_secret(settings), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()))["exp"] > time.time()
    except (ValueError, KeyError):
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN001
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):  # noqa: ANN201
        path = request.url.path
        if self.settings.app_password and path.startswith("/api/") and path not in OPEN_PATHS:
            if not valid_token(self.settings, request.cookies.get(COOKIE)):
                return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
        return await call_next(request)


class LoginIn(BaseModel):
    password: str


def auth_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/auth")
    failures: dict[str, list[float]] = {}
    lock = threading.Lock()

    @router.get("/status")
    def status(request: Request) -> dict:
        required = bool(settings.app_password)
        return {"required": required, "authenticated": not required or valid_token(settings, request.cookies.get(COOKIE))}

    @router.post("/login")
    def login(request: Request, response: Response, body: LoginIn) -> dict:
        if not settings.app_password:
            return {"ok": True}
        ip = request.client.host if request.client else "?"
        now = time.time()
        with lock:
            recent = [t for t in failures.get(ip, []) if now - t < LOCK_SECONDS]
            failures[ip] = recent
            if len(recent) >= MAX_FAILURES:
                raise HTTPException(status_code=429, detail="로그인 시도가 너무 많습니다. 10분 뒤에 다시 시도해 주세요")
        if not hmac.compare_digest(body.password.encode(), settings.app_password.encode()):
            with lock:
                failures.setdefault(ip, []).append(now)
            raise HTTPException(status_code=401, detail="비밀번호가 맞지 않습니다")
        with lock:
            failures.pop(ip, None)
        response.set_cookie(
            COOKIE,
            issue_token(settings),
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
        )
        return {"ok": True}

    @router.post("/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    return router
