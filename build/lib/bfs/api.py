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

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from . import security
from .analyze import KIND_BINARY, KIND_IMAGE, KIND_LABELS, analyze, normalize, render_html
from .config import Settings
from .ratelimit import SlidingWindow
from .service import FilePayload, Service, ShareError

router = APIRouter()


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
async def create_share(request: Request) -> JSONResponse:
    """建分享。Content-Type 是 multipart 就收文件，否则按 JSON 收文本。"""
    _check_rate(request, "create")
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


@router.get("/shares/{share_id}")
def share_meta(share_id: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    share = service.peek(share_id, base_url(request))
    share["unlocked"] = _unlocked(request, share_id)
    return share


@router.post("/shares/status")
def shares_status(body: StatusBody, request: Request) -> dict[str, Any]:
    """批量查状态（不消耗次数）。

    发送端历史用它把「待读取」刷成「已读」。只回状态字段，不回正文 ——
    这条路径本身不能成为绕过「阅后即焚」的窥视口。
    """
    service = get_service(request)
    items = []
    for share_id in dict.fromkeys(body.ids):  # 去重且保序
        try:
            share = service.peek(share_id)
        except ShareError:
            items.append({"id": share_id, "state": "deleted", "read_count": 0, "remaining": 0})
            continue
        items.append(
            {
                "id": share["id"],
                "state": share["state"],
                "read_count": share["read_count"],
                "remaining": share["remaining"],
                "expires_at": share["expires_at"],
            }
        )
    return {"items": items}


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
def burn(share_id: str, body: BurnBody, request: Request) -> dict[str, Any]:
    service = get_service(request)
    share = service.burn(share_id, body.manage_token, ip=client_ip(request))
    return share


@router.get("/shares/{share_id}/manage")
def manage(share_id: str, manage_token: str, request: Request) -> dict[str, Any]:
    service = get_service(request)
    data = service.stats(share_id, manage_token)
    data["share"]["url"] = get_settings(request).public_url(
        base_url(request), share_id
    )
    return data


@router.post("/shares/{share_id}/ota")
def create_ota(share_id: str, body: OtaBody, request: Request) -> dict[str, Any]:
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
