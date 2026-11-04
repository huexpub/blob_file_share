"""管理员登录：单密码 / GitHub OAuth2。

为什么要「登录」而不是「用户系统」
--------------------------------
这个服务只有一个作者。闸门只需要回答一个问题：**这个请求是不是作者本人**。
所以没有用户表、没有迁移、没有找回密码 —— 那些都是多用户身份系统的成本。

登录态就是一个签名 cookie（HMAC + ``data/secret.key``）：

    1751234567.YWRtaW4.<sig>

  * 无状态：进程重启不掉线，不需要会话表；
  * 防篡改：签名覆盖过期时间与身份，改一个字符就失效；
  * 密钥不在 cookie 里，泄了 cookie 也只能用到过期。

密码为什么不做哈希存储
--------------------
密码来自环境变量，本来就是明文躺在 ``.env`` / compose 里。再哈希一次只是把
「明文比较」变成「明文哈希比较」，安全性没有变化，却多出一个要维护的 salt。
真正挡暴力破解的是 ``ratelimit`` 里的每 IP 尝试上限。

GitHub 只是「你是哪个 GitHub 用户」
---------------------------------
任何 GitHub 账号都能走完 OAuth 流程，但只有 ``BFS_GITHUB_ALLOWED_USERS``
里的那个能拿到 session。所以即使 OAuth App 的 secret 泄露，攻击者也只能
证明自己是自己。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Settings

#: 登录态 cookie
SESSION_COOKIE = "bfs_session"
#: OAuth 回跳校验用的临时 cookie
STATE_COOKIE = "bfs_oauth_state"

_GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
_GITHUB_USER = "https://api.github.com/user"


class OAuthError(RuntimeError):
    """GitHub 那边失败了（网络、限流、code 无效）。"""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _sign(secret: bytes, payload: str) -> str:
    return _b64(hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest())


# ---------------------------------------------------------------- 登录态


def issue_session(secret: bytes, subject: str, ttl_seconds: int) -> str:
    expires = int(time.time()) + ttl_seconds
    token = _b64(subject.encode("utf-8"))
    return f"{expires}.{token}.{_sign(secret, f'session:{expires}.{subject}')}"


def session_subject(secret: bytes, raw: str | None) -> str | None:
    """校验 cookie，返回身份；无效返回 ``None``（不区分原因，别给探测线索）。"""
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    expires_raw, token, mac = parts
    try:
        expires = int(expires_raw)
    except ValueError:
        return None
    if expires < int(time.time()):
        return None
    try:
        subject = _unb64(token).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    expected = _sign(secret, f"session:{expires}.{subject}")
    if not hmac.compare_digest(expected, mac):
        return None
    return subject


# ---------------------------------------------------------------- 密码


def password_subject(settings: Settings) -> str:
    return settings.admin_user or "admin"


def check_password(settings: Settings, username: str, password: str) -> bool:
    expected = settings.admin_password
    if not expected:
        return False
    if settings.admin_user and not hmac.compare_digest(
        settings.admin_user.lower().encode("utf-8"), username.strip().lower().encode("utf-8")
    ):
        return False
    return hmac.compare_digest(expected.encode("utf-8"), password.encode("utf-8"))


# ---------------------------------------------------------------- OAuth


def new_state(secret: bytes) -> str:
    """``nonce.sig`` —— 防 CSRF：回跳时必须带回自己发出去的那个 nonce。"""
    nonce = secrets.token_urlsafe(16)
    return f"{nonce}.{_sign(secret, f'state:{nonce}')}"


def verify_state(secret: bytes, state: str | None) -> bool:
    if not state or "." not in state:
        return False
    nonce, mac = state.split(".", 1)
    return hmac.compare_digest(_sign(secret, f"state:{nonce}"), mac)


def authorize_url(settings: Settings, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": redirect_uri,
        "scope": "read:user",
        "state": state,
        "allow_signup": "false",
    }
    return f"{_GITHUB_AUTHORIZE}?{urllib.parse.urlencode(params)}"


def authorized_github_user(settings: Settings, login: str) -> bool:
    return login.strip().lower() in settings.github_allowed_set


def exchange_code(settings: Settings, code: str, redirect_uri: str) -> str:
    """code -> access_token -> GitHub login。

    阻塞调用（``urllib``），路由里用 ``asyncio.to_thread`` 包一层 ——
    为这一次请求引入 httpx 或 authlib 不值得。
    """
    if not code:
        raise OAuthError("缺少 code")

    body = urllib.parse.urlencode(
        {
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _GITHUB_TOKEN,
        data=body,
        headers={"Accept": "application/json", "User-Agent": "bfs"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            token_payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OAuthError(f"换取 token 失败：{exc}") from exc

    token = token_payload.get("access_token")
    if not token:
        raise OAuthError(f"GitHub 未返回 token：{token_payload.get('error')}")

    profile_request = urllib.request.Request(
        _GITHUB_USER,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "bfs",
        },
    )
    try:
        with urllib.request.urlopen(profile_request, timeout=15) as response:
            profile = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OAuthError(f"读取用户信息失败：{exc}") from exc

    login = profile.get("login")
    if not login:
        raise OAuthError("GitHub 未返回 login")
    return str(login)
