"""HTTP 层：只做参数校验 + 状态码映射，业务判定全在 ``service``。

接口一览：

    POST /api/shares                  建分享（JSON=文本 / multipart=文件）
    POST /api/inspect                 只识别不保存（粘贴后即时预览）
    GET  /api/shares/{id}             元数据（不消耗次数）
    POST /api/shares/{id}/unlock      校验提取码，下发解锁票据
    GET  /api/shares/{id}/content     取文本（消耗一次）
    GET  /api/shares/{id}/render      取渲染好的 HTML（消耗一次）
    GET  /api/shares/{id}/download    下载文件（消耗一次）
    POST /api/shares/{id}/burn        提前焚毁（带 manage_token）
    GET  /api/shares/{id}/manage      查看统计 / 已发出的 OTA 直链
    POST /api/shares/{id}/ota         建设备直链
    GET  /ota/{token}                 设备侧拉取（消耗直链次数）
    GET  /healthz                     探活

约定：错误统一是 ``{"error": "<code>", "message": "<中文说明>"}``，
前端靠 code 决定文案/交互，不解析中文。
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from . import auth, security, tokens
from .history import burn_owned, clear_owned, list_shares, status_items
from .analyze import KIND_BINARY, KIND_IMAGE, KIND_LABELS, analyze, normalize, render_html
from .config import Settings
from .ratelimit import SlidingWindow
from .service import FilePayload, Service, ShareError

router = APIRouter()

#: ?? CI ?????????
MAX_BATCH_FILES = 20


# ---------------------------------------------------------------- 请求体


class CreateTextBody(BaseModel):
    text: str = Field(min_length=1)
    ttl_hours: int | None = None
    max_reads: int | None = None
    #: 留空 = 走服务端默认（通常自动生成 4 位）；``no_code`` 才表示不要提取码。
    extract_code: str | None = None
    no_code: bool = False
    #: 强制指定形态（用户在预览里改了识别结果）；留空 = 自动识别
    kind: str | None = None


class UnlockBody(BaseModel):
    code: str = ""


class LoginBody(BaseModel):
    """密码登录。``username`` 只在配了 ``BFS_ADMIN_USER`` 时才参与校验。"""

    username: str = ""
    password: str = Field(max_length=512)


class BurnBody(BaseModel):
    manage_token: str


class OtaBody(BaseModel):
    manage_token: str
    label: str | None = None
    max_uses: int | None = None
    ttl_hours: int | None = None


class MarkdownBody(BaseModel):
    text: str = Field(max_length=512 * 1024)


class InspectBody(BaseModel):
    """粘贴预览。``force`` 让用户手动纠正识别结果。"""

    text: str = Field(max_length=4 * 1024 * 1024)
    force: str | None = None
    render: bool = True
    #: 可选的文件名：有扩展名时识别器优先信它（`.md` 就是 Markdown）
    filename: str | None = None


class StatusBody(BaseModel):
    """批量查状态。发送端历史用它把「待读取」自动变成「已读」。"""

    ids: list[str] = Field(default_factory=list, max_length=200)


class TokenBody(BaseModel):
    """创建 CI token。``label`` 只是给自己看的备注。"""

    label: str = Field(default="", max_length=64)


class BatchUploadBody(BaseModel):
    """可选的 multipart 描述字段；files 通过重复的 `file` 字段提交。"""

    ttl_hours: int | None = None
    max_reads: int | None = None
    extract_code: str | None = None
    no_code: bool = False


class CommitBody(BaseModel):
    """脚本用：关闭/消费确认。查询串同样接受 ``claim_token``（给 sendBeacon 用）。"""

    claim_token: str


# ---------------------------------------------------------------- 依赖


def get_service(request: Request) -> Service:
    return request.app.state.service


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_limiter(request: Request) -> SlidingWindow:
    return request.app.state.limiter


def client_ip(request: Request) -> str:
    # 反代场景下 X-Forwarded-For 的第一段才是客户端
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def base_url(request: Request) -> str:
    """请求的 origin（不含挂载前缀）。

    前缀由 ``Settings.share_path`` / ``api_prefix`` 负责拼接 —— 这里只回答
    「哪个域名」。反代转发时 ``request.base_url`` 本身就是对外域名，
    所以通常不用额外处理；要硬指定就用 ``BFS_BASE_URL``。
    """
    return str(request.base_url).rstrip("/")


def _check_rate(request: Request, bucket: str, limit: int | None = None) -> None:
    settings = get_settings(request)
    limiter = get_limiter(request)
    allowed = limit if limit is not None else settings.rate_limit_per_minute
    if not limiter.hit(f"{bucket}:{client_ip(request)}", allowed):
        raise ShareError("rate_limited", "请求过于频繁，请稍后再试", 429)


def _share_headers(share: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Share-State": share["state"],
        "X-Share-Remaining": "-1" if share["remaining"] is None else str(share["remaining"]),
    }
    return headers


def _content_disposition(file_name: str) -> str:
    ascii_name = file_name.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name)}"


def _secret(request: Request) -> bytes:
    return request.app.state.secret


def _unlocked(request: Request, share_id: str) -> bool:
    ticket = request.cookies.get(security.cookie_name(share_id))
    return security.verify_ticket(_secret(request), share_id, ticket)


# ---------------------------------------------------------------- 管理员闸门
#
# 只锁**写入口**：建分享、查历史、焚毁、OTA。
# 接收方（unlock / claim / commit / render / download / ota 拉取）永远公开 ——
# 对方手里有链接和提取码就该能看，让他注册账号是把产品做坏了。
#
# `/inspect` 也保持公开：它不落库、不写盘，而分享页渲染 .md/.log 正文时要靠它。
# 靠限速兜住「被当成免费 markdown 渲染器」的情况即可。


def _session(request: Request) -> str | None:
    raw = request.cookies.get(auth.SESSION_COOKIE)
    return auth.session_subject(_secret(request), raw)


def require_admin(request: Request) -> str:
    """写入口的依赖。未配置闸门时直接放行（本地开发不受影响）。"""
    settings = get_settings(request)
    if not settings.gate_enabled:
        return "anonymous"
    subject = _session(request)
    if not subject:
        raise ShareError("unauthorized", "需要先登录", 401)
    return subject


def _set_session(response: Response, request: Request, subject: str) -> None:
    settings = get_settings(request)
    ttl = settings.session_ttl_hours * 3600
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(_secret(request), subject, ttl),
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.get("/auth/session")
def auth_session(request: Request) -> dict[str, Any]:
    """前端启动时问一次：要不要显示登录、当前是谁。"""
    settings = get_settings(request)
    return {
        "gate": settings.gate_enabled,
        "authenticated": bool(_session(request)) or not settings.gate_enabled,
        "subject": _session(request),
        "username_required": bool(settings.admin_user),
        "methods": {
            "password": bool(settings.admin_password),
            "github": settings.github_ready,
            "gitea": settings.gitea_ready,
            "oauth": settings.oauth_enabled,
        },
        "login_path": settings.login_path,
    }


@router.post("/auth/login")
def auth_login(body: LoginBody, request: Request) -> JSONResponse:
    settings = get_settings(request)
    if not settings.gate_enabled:
        raise ShareError("gate_disabled", "服务未开启登录", 400)
    if not settings.admin_password:
        raise ShareError("password_disabled", "未配置密码登录，请用 GitHub 登录", 400)

    # 每 IP 每分钟 10 次：环境变量里的密码通常不短，但也不该给无限次尝试
    _check_rate(request, "login", 10)
    if not auth.check_password(settings, body.username, body.password):
        raise ShareError("bad_credentials", "用户名或密码不正确", 401)

    subject = auth.password_subject(settings)
    response = JSONResponse({"ok": True, "subject": subject})
    _set_session(response, request, subject)
    return response


@router.post("/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@router.get("/auth/github/start")
def auth_github_start(request: Request) -> Response:
    settings = get_settings(request)
    if not settings.github_ready:
        raise ShareError("github_disabled", "未配置 GitHub 登录", 400)
    state = auth.new_state(_secret(request))
    redirect_uri = settings.external_url(base_url(request), settings.oauth_callback_path)
    response = RedirectResponse(
        auth.authorize_url(settings, redirect_uri, state), status_code=302
    )
    response.set_cookie(
        auth.STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/auth/github/callback")
async def auth_github_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> Response:
    """GitHub 回跳。

    任何一步失败都回登录页并带上 ``?error=``，不在这里抛 4xx ——
    用户看到的是一个页面，而不是一段 JSON。
    """
    settings = get_settings(request)
    login_url = settings.login_path

    if error:
        return RedirectResponse(f"{login_url}?error=denied", status_code=302)
    if not auth.verify_state(_secret(request), state) or state != request.cookies.get(
        auth.STATE_COOKIE
    ):
        return RedirectResponse(f"{login_url}?error=state", status_code=302)

    redirect_uri = settings.external_url(base_url(request), settings.oauth_callback_path)
    try:
        login = await asyncio.to_thread(auth.exchange_code, settings, code, redirect_uri)
    except auth.OAuthError:
        return RedirectResponse(f"{login_url}?error=oauth", status_code=302)

    if not auth.authorized_github_user(settings, login):
        return RedirectResponse(f"{login_url}?error=not_allowed", status_code=302)

    response = RedirectResponse(f"{settings.prefix}/" if settings.prefix else "/", status_code=302)
    _set_session(response, request, f"github:{login}")
    response.delete_cookie(auth.STATE_COOKIE, path="/")
    return response


@router.get("/auth/gitea/start")
def auth_gitea_start(request: Request) -> Response:
    settings = get_settings(request)
    if not settings.gitea_ready:
        raise ShareError("gitea_disabled", "未配置 Gitea 登录", 400)
    state = auth.new_state(_secret(request))
    redirect_uri = settings.external_url(base_url(request), f"{settings.api_prefix}/auth/gitea/callback")
    response = RedirectResponse(
        auth.gitea_authorize_url(settings, redirect_uri, state), status_code=302
    )
    response.set_cookie(
        auth.STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/auth/gitea/callback")
async def auth_gitea_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> Response:
    settings = get_settings(request)
    login_url = settings.login_path
    if error:
        return RedirectResponse(f"{login_url}?error=denied", status_code=302)
    if not auth.verify_state(_secret(request), state) or state != request.cookies.get(
        auth.STATE_COOKIE
    ):
        return RedirectResponse(f"{login_url}?error=state", status_code=302)

    redirect_uri = settings.external_url(
        base_url(request), f"{settings.api_prefix}/auth/gitea/callback"
    )
    try:
        login = await asyncio.to_thread(auth.exchange_gitea_code, settings, code, redirect_uri)
    except auth.OAuthError:
        return RedirectResponse(f"{login_url}?error=oauth", status_code=302)

    response = RedirectResponse(f"{settings.prefix}/" if settings.prefix else "/", status_code=302)
    _set_session(response, request, f"gitea:{login}")
    response.delete_cookie(auth.STATE_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- 路由
#
# 这里的路径都是**相对段**（`/shares`、`/ota/{token}`），不带 `/api`。
# 挂载点由 `create_app` 决定：`app.include_router(router, prefix=settings.api_prefix)`，
# 于是同一套路由在「根部署」是 `/api/shares`，在「子目录部署」是 `/bfs/api/shares`。
# 好处是永远不用在路由里拼前缀 —— 这类拼接最容易漏一处，然后某天 404。


@router.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    settings = get_settings(request)
    return {
        "ok": True,
        "service": "bfs",
        "prefix": settings.prefix,
        "share_prefix": settings.share_prefix,
    }


@router.get("/limits")
def limits(request: Request) -> dict[str, Any]:
    """前端用它决定提示文案与本地预校验（服务端仍然会再校验一遍）。"""
    settings = get_settings(request)
    return {
        "max_file_bytes": settings.max_file_bytes,
        "max_text_bytes": settings.max_text_bytes,
        "default_ttl_hours": settings.default_ttl_hours,
        "max_ttl_hours": settings.max_ttl_hours,
        "default_max_reads": settings.default_max_reads,
        "max_reads": settings.max_reads_limit,
        "ota_default_uses": settings.ota_default_uses,
        "prefix": settings.prefix,
        "share_prefix": settings.share_prefix,
    }


@router.post("/render/markdown")
def render_markdown(body: MarkdownBody) -> dict[str, str]:
    """Markdown -> HTML。

    放服务端而不是前端引第三方库：分享页要保持「零依赖静态文件」，
    而且渲染结果一致（浏览器里的 marked 版本各家不同）。
    安全：禁用原始 HTML（``html=False``），只让 markdown-it 生成标签，
    否则分享内容里塞一段 <script> 就是存储型 XSS。
    """
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
    md.enable("table").enable("strikethrough")
    return {"html": md.render(body.text)}


@router.post("/inspect")
def inspect(body: InspectBody) -> dict[str, Any]:
    """只识别 + 渲染，不落库、不消耗次数。

    两个调用方：
      * 发送端：粘贴后 200ms 防抖调一次，边打字边看「会发成什么样」；
      * 接收端：分享的是**文件**时（.md / .txt / .log / .py …），
        读成字符串后借这里渲染，省得把识别器在前端再写一遍。

    ``filename`` 参与识别：有扩展名就优先信它（`.md` 就是 Markdown，
    内容里有没有 `#` 都算）。文件路径没扩展到名的话，`.` 开头的 Markdown
    会被当成纯文本 —— 所以接收端必须把文件名传进来。
    """
    text = normalize(body.text)
    if not text.strip():
        raise ShareError("empty", "内容为空")
    analysis = analyze(text, filename=body.filename, force=body.force)
    payload: dict[str, Any] = {
        "detected": analysis.to_dict(),
        "kinds": [
            {"kind": key, "label": label}
            for key, label in KIND_LABELS.items()
            if key not in {KIND_IMAGE, KIND_BINARY}
        ],
    }
    if body.render:
        # 预览只渲染前 200 KB：粘贴板里塞进来的东西可能很大，
        # 首屏不需要等全量渲染。
        payload["html"] = render_html(text[: 200 * 1024], analysis)
        payload["truncated"] = len(text) > 200 * 1024
    return payload


@router.post("/shares")
async def create_share(request: Request, admin: str = Depends(require_admin)) -> JSONResponse:
    """建单个文本/文件分享（网页现有流程）。"""
    _check_rate(request, "create")
    owner = admin if admin != "anonymous" else None
    service = get_service(request)
    settings = get_settings(request)
    content_type = request.headers.get("content-type", "")
    base = base_url(request)

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ShareError("no_file", "缺少 file 字段")
        created = service.create_file(
            file_name=upload.filename or "download.bin",
            data=upload.file,
            content_type=upload.content_type or "application/octet-stream",
            ttl_hours=_int_field(form.get("ttl_hours")),
            max_reads=_int_field(form.get("max_reads")),
            extract_code=_str_field(form.get("extract_code")),
            no_code=_bool_field(form.get("no_code")),
            ip=client_ip(request),
            base_url=base,
            owner_id=owner,
        )
    else:
        raw = await request.json()
        body = CreateTextBody(**raw)
        created = service.create_text(
            body.text,
            ttl_hours=body.ttl_hours,
            max_reads=body.max_reads,
            extract_code=body.extract_code,
            no_code=body.no_code,
            kind=body.kind,
            ip=client_ip(request),
            base_url=base,
            owner_id=owner,
        )

    payload = {
        **created.share,
        "manage_token": created.manage_token,
        "extract_code": created.extract_code,
        "limits": {
            "max_file_bytes": settings.max_file_bytes,
            "max_text_bytes": settings.max_text_bytes,
            "max_ttl_hours": settings.max_ttl_hours,
            "max_reads": settings.max_reads_limit,
        },
    }
    return JSONResponse(payload, status_code=201)


@router.get("/tokens")
def list_api_tokens(
    request: Request, admin: str = Depends(require_admin)
) -> dict[str, Any]:
    """列自己的 CI token。只回元信息 —— 明文永远不会再出现。"""
    if not get_settings(request).oauth_enabled:
        raise ShareError("oauth_required", "API token 需要启用 OAuth 登录", 403)
    return {
        "owner": admin,
        "items": tokens.list_tokens(get_service(request), admin),
        "max": tokens.MAX_TOKENS_PER_OWNER,
    }


@router.post("/tokens")
def create_api_token(
    body: TokenBody, request: Request, admin: str = Depends(require_admin)
) -> JSONResponse:
    """创建 token。明文只在这一条响应里出现一次。"""
    if not get_settings(request).oauth_enabled:
        raise ShareError("oauth_required", "API token 需要启用 OAuth 登录", 403)
    created = tokens.create_token(get_service(request), admin, body.label)
    return JSONResponse(created, status_code=201)


@router.delete("/tokens")
def revoke_api_tokens(
    request: Request,
    admin: str = Depends(require_admin),
    label: str | None = None,
) -> dict[str, int]:
    """撤销 token。带 ``?label=`` 只撤销一个，不带则全部撤销。"""
    if not get_settings(request).oauth_enabled:
        raise ShareError("oauth_required", "API token 需要启用 OAuth 登录", 403)
    return {"revoked": tokens.revoke_tokens(get_service(request), admin, label)}


@router.get("/shares/mine")
def owned_shares(
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """当前账户的分享历史：网页端和 CI 上传的都算在内。"""
    settings = get_settings(request)
    if not settings.oauth_enabled:
        raise ShareError("managed_history_disabled", "未启用 OAuth 登录", 403)
    return {
        "managed": True,
        "owner": admin,
        "items": list_shares(get_service(request), admin, base_url(request)),
    }


@router.get("/shares/{share_id}")
def share_meta(share_id: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    share = service.peek(share_id, base_url(request))
    share["unlocked"] = _unlocked(request, share_id)
    return share


@router.post("/shares/batch")
async def create_batch_share(request: Request) -> JSONResponse:
    """CI ??????? Bearer token???????????

    ?????????
      1. ????? OAuth ?? ?????????????? token ????
      2. ?????? Bearer token?
      3. token ?????

    ???????? = token ??????? CI ????????????
    ??????????????????????????
    """
    settings = get_settings(request)
    if not settings.oauth_enabled:
        raise ShareError(
            "oauth_required", "CI 上传需要启用 OAuth 登录（GitHub 或 Gitea）", 403
        )

    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    owner = (
        tokens.resolve_owner(get_service(request), credential.strip())
        if scheme.lower() == "bearer"
        else None
    )
    if not owner:
        raise ShareError("unauthorized", "API token 无效或已被撤销", 401)

    _check_rate(request, "api_batch")
    form = await request.form()
    uploads = [item for item in form.getlist("file") if isinstance(item, UploadFile)]
    if not uploads:
        raise ShareError("no_file", "至少上传一个 file 字段", 400)
    if len(uploads) > MAX_BATCH_FILES:
        raise ShareError(
            "too_many_files", f"一次最多上传 {MAX_BATCH_FILES} 个文件", 413
        )

    ttl_hours = _int_field(form.get("ttl_hours"))
    if ttl_hours is None:
        ttl_hours = settings.api_default_ttl_hours
    max_reads = _int_field(form.get("max_reads"))
    if max_reads is None:
        max_reads = -1
    extract_code = _str_field(form.get("extract_code"))
    no_code = _bool_field(form.get("no_code"))

    service = get_service(request)
    created = []
    for upload in uploads:
        item = service.create_file(
            file_name=upload.filename or "download.bin",
            data=upload.file,
            content_type=upload.content_type or "application/octet-stream",
            ttl_hours=ttl_hours,
            max_reads=max_reads,
            extract_code=extract_code,
            no_code=no_code,
            ip=client_ip(request),
            base_url=base_url(request),
            owner_id=owner,
        )
        created.append({
            **item.share,
            "manage_token": item.manage_token,
            "extract_code": item.extract_code,
        })

    return JSONResponse(
        {"items": created, "message": _batch_share_message(created), "owner": owner},
        status_code=201,
    )


def _batch_share_message(items: list[dict[str, Any]]) -> str:
    if len(items) == 1:
        item = items[0]
        label = item.get("file_name") or item.get("detected", {}).get("kind_label") or "文件"
        lines = [f"我分享给你一个文件（{label}）：", item["url"]]
        if item.get("extract_code"):
            lines.append(f"分享码 {item['extract_code']}")
        if item.get("max_reads") == 1:
            lines.append("阅后即焚，打开看完就没了。")
        return "\n".join(lines)

    lines = [f"我分享给你 {len(items)} 个文件："]
    for index, item in enumerate(items, 1):
        label = item.get("file_name") or "文件"
        lines.extend([f"{index}. {label}", f"   {item['url']}"])
        if item.get("extract_code"):
            lines.append(f"   分享码 {item['extract_code']}")
    if any(item.get("max_reads") == 1 for item in items):
        lines.append("（标 1 次的是阅后即焚，打开看完就没了）")
    return "\n".join(lines)


@router.delete("/shares/mine")
def clear_owned_shares(request: Request, admin: str = Depends(require_admin)) -> dict[str, int]:
    if not get_settings(request).oauth_enabled:
        raise ShareError("managed_history_disabled", "未启用 OAuth 登录", 403)
    return {
        "burned": clear_owned(get_service(request), admin, ip=client_ip(request))
    }


@router.delete("/shares/{share_id}")
def delete_owned_share(
    share_id: str,
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    """历史删除即焚毁；只能操作自己名下的内容。"""
    if not get_settings(request).oauth_enabled:
        raise ShareError("managed_history_disabled", "未启用 OAuth 登录", 403)
    return burn_owned(
        get_service(request), admin, share_id, ip=client_ip(request)
    )


@router.post("/shares/status")
def shares_status(
    body: StatusBody, request: Request, admin: str = Depends(require_admin)
) -> dict[str, Any]:
    """批量查状态。OAuth 模式下只回答本人名下的条目。"""
    owner = admin if get_settings(request).oauth_enabled else None
    return {"items": status_items(get_service(request), owner, list(dict.fromkeys(body.ids)))}


@router.post("/shares/{share_id}/unlock")
def unlock(share_id: str, body: UnlockBody, request: Request) -> JSONResponse:
    settings = get_settings(request)
    service = get_service(request)
    # 每 IP 对每份分享 1 分钟最多 10 次，4 位提取码 1 万种组合 -> 需要 16 小时以上
    _check_rate(request, f"unlock:{share_id}", 10)
    view, _ = service.open_share(
        share_id,
        code=body.code,
        consume=False,
        ip=client_ip(request),
        base_url=base_url(request),
    )
    ticket = security.issue_ticket(
        _secret(request), share_id, settings.access_ttl_minutes * 60
    )
    response = JSONResponse({**view, "unlocked": True})
    response.set_cookie(
        security.cookie_name(share_id),
        ticket,
        max_age=settings.access_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.post("/shares/{share_id}/claim")
def claim(share_id: str, request: Request, code: str | None = None) -> Response:
    """预览：把正文交给浏览器，但**不扣次数、不删盘**。

    真正的消费在 ``/commit`` —— 接收端关掉弹窗时才调。这样「打开看一眼
    就关掉」在语义上才等于「看完」，而不是「一打开就烧掉」。
    """
    service = get_service(request)
    view, payload, claim_token = service.claim(
        share_id,
        code=code,
        verified=_unlocked(request, share_id),
        ip=client_ip(request),
        base_url=base_url(request),
    )

    if isinstance(payload, FilePayload):
        headers = _share_headers(view)
        headers["X-Claim-Token"] = claim_token
        headers["Content-Disposition"] = "inline"
        return FileResponse(
            payload.path,
            media_type=payload.content_type,
            headers=headers,
            background=BackgroundTask(service.release, payload),
        )

    response = PlainTextResponse(
        payload if isinstance(payload, str) else "",
        headers={**_share_headers(view), "X-Claim-Token": claim_token},
    )
    response.headers["Content-Type"] = view["content_type"]
    return response


@router.post("/shares/{share_id}/commit")
async def commit(share_id: str, request: Request, claim_token: str | None = None) -> dict[str, Any]:
    """确认消费：扣次数，次数用尽就删盘。

    `claim_token` 既可以走查询串也可以走 JSON body：
    浏览器在 `pagehide` 时只能用 ``navigator.sendBeacon``（无法自定义 JSON 头），
    所以查询串这条路径是给「关闭页面」用的；JSON body 保留给脚本调用。
    """
    service = get_service(request)
    token = claim_token
    if not token:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 —— 空 body / 非 JSON 都当作没带
            payload = None
        if isinstance(payload, dict):
            token = payload.get("claim_token")
    if not token:
        raise ShareError("bad_request", "缺少 claim_token", 400)
    return service.commit(share_id, str(token), ip=client_ip(request))


@router.get("/shares/{share_id}/content")
def share_content(
    share_id: str,
    request: Request,
    code: str | None = None,
) -> PlainTextResponse:
    """直接取文本正文并**立即消费一次**（脚本 / curl 用）。

    网页接收端走的是 ``claim`` + ``commit`` 两阶段（打开不扣、关闭才扣），
    这条是给「一次 GET 就要结果」的调用方准备的，语义是取完即焚。
    """
    service = get_service(request)
    view = service.peek(share_id, base_url(request))
    if view["kind"] != "text":
        raise ShareError("wrong_kind", "这不是文本分享", 400)

    view, payload = service.open_share(
        share_id,
        code=code,
        verified=_unlocked(request, share_id),
        ip=client_ip(request),
        base_url=base_url(request),
    )
    text = payload if isinstance(payload, str) else ""
    response = PlainTextResponse(text, headers=_share_headers(view))
    response.headers["Content-Type"] = view["content_type"]
    return response


@router.get("/shares/{share_id}/render")
def share_render(
    share_id: str,
    request: Request,
    code: str | None = None,
) -> JSONResponse:
    """取「按识别结果渲染好的 HTML」，消耗一次。

    为什么不给前端原始文本让它自己渲染：渲染器和识别器必须成套 ——
    服务端判定这是「日志」，就该用日志的样子画出来；两边各写一套必然跑偏。
    """
    service = get_service(request)
    view, payload = service.open_share(
        share_id,
        code=code,
        verified=_unlocked(request, share_id),
        ip=client_ip(request),
        base_url=base_url(request),
    )
    if isinstance(payload, FilePayload):
        raise ShareError("wrong_kind", "这是文件分享，请走下载", 400)

    text = normalize(payload or "")
    detected = view["detected"]
    analysis = analyze(text, force=detected.get("kind"))
    if detected.get("language") and analysis.kind == "code":
        analysis.language = detected["language"]
    return JSONResponse(
        {
            "share": view,
            "detected": analysis.to_dict(),
            "html": render_html(text, analysis),
            "text": text if len(text) <= 2 * 1024 * 1024 else None,
        },
        headers={"Cache-Control": "no-store"},
        status_code=200,
    )


@router.get("/shares/{share_id}/download")
def share_download(
    share_id: str,
    request: Request,
    code: str | None = None,
    inline: bool = False,
) -> Response:
    service = get_service(request)
    view, payload = service.open_share(
        share_id,
        code=code,
        verified=_unlocked(request, share_id),
        ip=client_ip(request),
        base_url=base_url(request),
    )

    if isinstance(payload, FilePayload):
        headers = _share_headers(view)
        headers["Content-Disposition"] = (
            "inline" if inline else _content_disposition(payload.file_name)
        )
        return FileResponse(
            payload.path,
            media_type=payload.content_type,
            headers=headers,
            background=BackgroundTask(service.release, payload),
        )
    # 文本分享也能走 download：直接以附件形式吐文本
    text = payload if isinstance(payload, str) else ""
    response = PlainTextResponse(text, headers=_share_headers(view))
    response.headers["Content-Type"] = view["content_type"]
    response.headers["Content-Disposition"] = _content_disposition(
        view["file_name"] or f"{view['id']}.txt"
    )
    return response


@router.post("/shares/{share_id}/burn")
def burn(
    share_id: str, body: BurnBody, request: Request, _admin: str = Depends(require_admin)
) -> dict[str, Any]:
    service = get_service(request)
    share = service.burn(share_id, body.manage_token, ip=client_ip(request))
    return share


@router.get("/shares/{share_id}/manage")
def manage(
    share_id: str,
    manage_token: str,
    request: Request,
    _admin: str = Depends(require_admin),
) -> dict[str, Any]:
    service = get_service(request)
    data = service.stats(share_id, manage_token)
    data["share"]["url"] = get_settings(request).public_url(
        base_url(request), share_id
    )
    return data


@router.post("/shares/{share_id}/ota")
def create_ota(
    share_id: str, body: OtaBody, request: Request, _admin: str = Depends(require_admin)
) -> dict[str, Any]:
    _check_rate(request, "ota")
    service = get_service(request)
    return service.create_ota(
        share_id,
        body.manage_token,
        label=body.label,
        max_uses=body.max_uses,
        ttl_hours=body.ttl_hours,
        ip=client_ip(request),
        base_url=base_url(request),
    )


#: 设备直链**不带 `/api`**：它是要写进固件/烧录脚本里的地址，越短越好。
#: 但同样要跟着挂载前缀走（`/bfs/ota/<token>`），所以由 create_app 一起挂载。
ota_router = APIRouter()


@ota_router.api_route("/ota/{token}", methods=["GET", "HEAD"])
def ota_pull(token: str, request: Request) -> Response:
    service = get_service(request)
    if request.method == "HEAD":
        # 设备先探活再下载：探活不扣次数
        share = service.peek_ota(token)
        return Response(
            status_code=200,
            headers={
                "Content-Length": str(share["size"]),
                "Content-Type": share["content_type"],
                "X-Share-Sha256": share["sha256"],
                "X-Ota-Remaining": str(share["ota_max_uses"] - share["ota_uses"]),
            },
        )
    view, payload = service.consume_ota(token, ip=client_ip(request))
    return FileResponse(
        payload.path,
        media_type=payload.content_type,
        headers={
            **_share_headers(view),
            "Content-Disposition": _content_disposition(payload.file_name),
            "X-Share-Sha256": view["sha256"],
            "X-Share-Md5": view["md5"],
            "X-Share-Crc32": view["crc32"],
        },
        background=BackgroundTask(service.release, payload),
    )


# ---------------------------------------------------------------- 组装


def _int_field(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ShareError("bad_param", "数值参数格式不正确") from None


def _str_field(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_field(value: Any) -> bool:
    """multipart 里的布尔：`1` / `true` / `on` / `yes` 都算真。"""
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ShareError)
    async def _share_error(_: Request, exc: ShareError) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        if exc.code == "rate_limited":
            headers["Retry-After"] = "60"
        return JSONResponse(
            {"error": exc.code, "message": exc.message},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(413)
    async def _too_large(_: Request, __) -> JSONResponse:
        return JSONResponse(
            {"error": "too_large", "message": "上传内容超过服务端上限"},
            status_code=413,
        )
