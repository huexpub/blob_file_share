"""应用装配。

FastAPI + 单进程内限速 + SQLite。前端是仓库里的静态文件（无需构建），
由 ``web.py`` 直接吐出去 —— 部署时只有一个 Python 进程。
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from . import __version__
from .api import ota_router, register_error_handlers, router
from .config import Settings
from .db import init_db
from .ratelimit import SlidingWindow
from .service import Service
from .web import build_web_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    init_db(settings.db_path)
    service = Service(settings)

    app = FastAPI(
        title="BFS",
        version=__version__,
        description="阅后即焚的文本 / 文件快传服务",
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        lifespan=_make_lifespan(settings, service),
    )
    app.state.settings = settings
    app.state.service = service
    app.state.secret = service.secret
    app.state.limiter = SlidingWindow()

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    register_error_handlers(app)
    app.include_router(router, prefix=settings.api_prefix)
    app.include_router(ota_router, prefix=settings.prefix)
    app.include_router(build_web_router(settings))
    return app


def _make_lifespan(settings: Settings, service: Service):
    """内置清理任务放在 lifespan 里：不依赖外部 cron，也不碰已弃用的 on_event。

    FastAPI 的 ``lifespan`` 会在 start 前 / stop 后执行；这里只用后半段 ——
    起一个后台 task 周期扫过期分享，退出时取消。
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        interval = max(30, settings.purge_interval_seconds)

        async def loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await asyncio.to_thread(service.purge)
                except Exception:  # noqa: BLE001 —— 清理失败不能拖垮服务
                    pass

        task = asyncio.create_task(loop())
        app.state.janitor = task
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return lifespan
