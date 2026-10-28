from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.auth import AuthMiddleware, auth_router
from app.config import Settings, get_settings
from app.container import Container, build_container

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        c = container or build_container(settings)
        app.state.container = c
        c.start_loop()
        try:
            yield
        finally:
            c.stop_loop()

    app = FastAPI(title=f"{settings.app_name} API", lifespan=lifespan)
    if not settings.app_password:
        logging.getLogger(__name__).warning("APP_PASSWORD 가 없어 로그인 없이 열려 있습니다. 공개 도메인에서는 반드시 설정하세요.")
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(auth_router(settings))
    app.include_router(router)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    dist: Path = settings.frontend_dist
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        no_cache = {"Cache-Control": "no-cache"}

        @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        def spa(path: str) -> FileResponse:
            target = (dist / path).resolve()
            if path and target.is_file() and dist.resolve() in target.parents:
                if target.name == "sw.js":
                    # 서비스워커는 항상 새로 받아야 업데이트가 반영됩니다.
                    return FileResponse(target, media_type="text/javascript", headers=no_cache | {"Service-Worker-Allowed": "/"})
                if target.suffix == ".webmanifest":
                    return FileResponse(target, media_type="application/manifest+json", headers=no_cache)
                return FileResponse(target)
            return FileResponse(dist / "index.html", headers=no_cache)

    return app


app = create_app()
