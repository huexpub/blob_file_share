"""页面路由。

前端是**手写静态文件**（`bfs/static/`），不引入构建链。

路径布局由 ``BFS_PATH_PREFIX`` 决定（见 ``config.Settings.prefix``）：

    path_prefix = ""         path_prefix = "bfs"
    /                        /bfs
    /s/<id>                  /bfs/<id>
    /api/...                 /bfs/api/...
    /static/app.js           /bfs/static/app.js

为什么要连根路径一起搬走：nginx 把 ``view.lotenall.com/bfs`` 整段转发进来时，
页面里的相对资源、fetch 的 `/api`、分享链接都必须落在同一个前缀里，
否则会漏到主站去。所以前缀不是「加个路由」而是**整个服务搬进子目录** ——
页面里所有地址都从 ``window.__BFS_BASE__`` 读，不再硬编码 ``/api``。
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from .config import STATIC_DIR

_MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".woff2": "font/woff2",
}


def _media_type(path: Path) -> str:
    # 显式给 .js/.css 定类型：Windows 注册表里 .js 的 MIME 常被改成 text/plain，
    # 而 ES module 会因为 MIME 不对被浏览器整段拒绝执行。
    return (
        _MEDIA_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


def _page(name: str, settings) -> HTMLResponse:
    """读页面，并把「本服务挂在哪」注入进去。

    两件事：
      1. `__BASE__` 占位符换成实际前缀 —— 页面里的 `<link>` / `<script>`
         不能用相对路径，因为分享页的 URL 深度不固定（`/bfs/<id>` 与 `/s/<id>`），
         相对路径在两种部署下解析结果不同，一定会有一边 404。
      2. 注入 `window.__BFS_*__`，前端所有 API / 跳转地址都从它拼。

    这样同一份 HTML 既能挂在根上，也能挂在 `/bfs` 下，不需要两套产物。
    """
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    base = settings.prefix  # "" 或 "/bfs"
    boot = {
        "base": base,
        "sharePrefix": settings.share_prefix,
        "apiPrefix": settings.api_prefix,
    }
    inject = (
        f"<script>window.__BFS_BASE__={json.dumps(boot['base'])};"
        f"window.__BFS_SHARE_PREFIX__={json.dumps(boot['sharePrefix'])};"
        f"window.__BFS_API_PREFIX__={json.dumps(boot['apiPrefix'])};</script>"
    )
    html = html.replace("__BASE__", base)
    # 注入必须在加载 app.js 之前：app.js 里算 API 地址时就用了
    html = html.replace('<link rel="stylesheet"', f'{inject}\n    <link rel="stylesheet"', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def build_web_router(settings) -> APIRouter:
    """页面 / 静态资源路由。

    路径全部由 ``Settings`` 派生，所以「根部署」和「子目录部署」用同一套代码：

        path_prefix=""     ->  /、/s/<id>、/static/app.js
        path_prefix="bfs"  ->  /bfs、/bfs/<id>、/bfs/static/app.js
        share_segment="s"  ->  /bfs/s/<id>（分享页单独一段）
    """
    router = APIRouter()
    prefix = settings.prefix
    # share_prefix 已经含挂载前缀，直接当完整路径用
    share_prefix = settings.share_prefix

    @router.get(f"{prefix}/" if prefix else "/", include_in_schema=False)
    def index() -> HTMLResponse:
        return _page("index.html", settings)

    @router.get(f"{share_prefix}/{{share_id}}", include_in_schema=False)
    def share_page(share_id: str) -> HTMLResponse:
        return _page("share.html", settings)

    @router.get(f"{prefix}/favicon.svg" if prefix else "/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @router.get(f"{prefix}/static/{{path:path}}", include_in_schema=False)
    def static_file(path: str) -> FileResponse:
        root = STATIC_DIR.resolve()
        target = (STATIC_DIR / path).resolve()
        if root not in target.parents or not target.is_file():
            return RedirectResponse(f"{prefix}/" if prefix else "/", status_code=307)
        return FileResponse(
            target,
            media_type=_media_type(target),
            headers={"Cache-Control": "no-cache"},
        )

    return router
