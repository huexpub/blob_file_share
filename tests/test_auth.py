"""管理员闸门：默认公开、配了密码/ GitHub 才锁写入口。

重点验证的不是「密码对不对」，而是**哪些端点被锁、哪些没被锁** ——
接收方必须始终能匿名打开链接，这是产品语义，不是安全妥协。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from bfs import auth
from bfs.app import create_app
from bfs.config import Settings


def make_client(tmp_path, **kwargs) -> TestClient:
    settings = Settings(data_dir=tmp_path / "data", rate_limit_per_minute=1000, **kwargs)
    return TestClient(create_app(settings))


def share_payload() -> dict:
    return {"text": "hello", "kind": "text", "no_code": True}


# ---------------------------------------------------------------- 公开模式


def test_no_password_keeps_write_open(tmp_path):
    """不配任何登录方式 = 保持原有公开行为（本地开发/内网不受影响）。"""
    with make_client(tmp_path) as client:
        session = client.get("/api/auth/session").json()
        assert session["gate"] is False
        assert session["authenticated"] is True
        assert client.post("/api/shares", json=share_payload()).status_code == 201


# ---------------------------------------------------------------- 密码


def test_password_gate_locks_write_endpoints(tmp_path):
    with make_client(tmp_path, admin_password="s3cret") as client:
        assert client.get("/api/auth/session").json()["gate"] is True

        for path, body in (
            ("/api/shares", share_payload()),
            ("/api/shares/status", {"ids": ["x"]}),
        ):
            response = client.post(path, json=body)
            assert response.status_code == 401, path
            assert response.json()["error"] == "unauthorized"


def test_password_login_unlocks_and_logout_relocks(tmp_path):
    with make_client(tmp_path, admin_password="s3cret") as client:
        bad = client.post("/api/auth/login", json={"username": "", "password": "nope"})
        assert bad.status_code == 401
        assert bad.json()["error"] == "bad_credentials"

        ok = client.post("/api/auth/login", json={"username": "", "password": "s3cret"})
        assert ok.status_code == 200
        assert client.post("/api/shares", json=share_payload()).status_code == 201

        client.post("/api/auth/logout")
        assert client.post("/api/shares", json=share_payload()).status_code == 401


def test_username_is_required_when_configured(tmp_path):
    with make_client(tmp_path, admin_password="p", admin_user="huex") as client:
        wrong = client.post("/api/auth/login", json={"username": "other", "password": "p"})
        assert wrong.status_code == 401
        # 大小写不敏感：手打用户名不该被大小写卡住
        right = client.post("/api/auth/login", json={"username": "HUEX", "password": "p"})
        assert right.status_code == 200


def test_receive_side_stays_public(tmp_path):
    """闸门只锁写入口：拿链接 + 提取码的人不需要账号。"""
    with make_client(tmp_path, admin_password="s3cret") as client:
        admin = make_client(tmp_path)
        with admin:
            admin.post("/api/auth/login", json={"username": "", "password": "s3cret"})
            created = admin.post("/api/shares", json={"text": "code here", "kind": "text"})
            share_id = created.json()["id"]
            code = created.json()["extract_code"]

        assert client.get(f"/api/shares/{share_id}").status_code == 200
        unlocked = client.post(f"/api/shares/{share_id}/unlock", json={"code": code})
        assert unlocked.status_code == 200
        claim = client.post(f"/api/shares/{share_id}/claim")
        assert claim.status_code == 200
        assert claim.text == "code here"
        assert client.post(f"/api/shares/{share_id}/commit", json={
            "claim_token": claim.headers["X-Claim-Token"]
        }).status_code == 200


def test_inspect_stays_public(tmp_path):
    """分享页渲染 .md/.log 正文要用 inspect，锁了接收端就没法排版。"""
    with make_client(tmp_path, admin_password="s3cret") as client:
        response = client.post("/api/inspect", json={"text": "# 标题\n\n正文段落。\n\n- 项目一\n- 项目二"})
        assert response.status_code == 200
        assert "prose" in response.json()["html"]


def test_login_page_is_served(tmp_path):
    with make_client(tmp_path, admin_password="s3cret", path_prefix="bfs") as client:
        page = client.get("/bfs/login")
        assert page.status_code == 200
        assert "登录" in page.text
        # 注入的 boot 常量要带前缀，否则登录页的 fetch 会漏到主站
        assert 'window.__BFS_API_PREFIX__="/bfs/api"' in page.text
        assert 'window.__BFS_GATE__=true' in page.text


# ---------------------------------------------------------------- GitHub


def test_github_requires_all_three_settings(tmp_path):
    """少一个就当作没配 —— 免得用户卡在 GitHub 的错误页上。"""
    partial = Settings(
        data_dir=tmp_path / "d1",
        github_client_id="id",
        github_client_secret="secret",
    )
    assert partial.github_ready is False
    assert partial.gate_enabled is False

    full = Settings(
        data_dir=tmp_path / "d2",
        github_client_id="id",
        github_client_secret="secret",
        github_allowed_users="huexpub",
    )
    assert full.github_ready is True
    assert full.gate_enabled is True


def test_github_start_redirects_with_state(tmp_path):
    with make_client(
        tmp_path,
        github_client_id="id",
        github_client_secret="secret",
        github_allowed_users="huexpub",
    ) as client:
        response = client.get("/api/auth/github/start", follow_redirects=False)
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://github.com/login/oauth/authorize?")
        assert "client_id=id" in location
        assert "state=" in location
        assert response.cookies.get(auth.STATE_COOKIE)


def test_github_callback_rejects_bad_state(tmp_path):
    with make_client(
        tmp_path,
        github_client_id="id",
        github_client_secret="secret",
        github_allowed_users="huexpub",
    ) as client:
        response = client.get(
            "/api/auth/github/callback?code=abc&state=forged.sig",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"].endswith("/login?error=state")


def test_github_callback_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "exchange_code", lambda *a, **k: "huexpub")
    with make_client(
        tmp_path,
        github_client_id="id",
        github_client_secret="secret",
        github_allowed_users="HuexPub",
    ) as client:
        started = client.get("/api/auth/github/start", follow_redirects=False)
        state = started.cookies.get(auth.STATE_COOKIE)

        response = client.get(
            f"/api/auth/github/callback?code=abc&state={state}", follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/"
        assert client.post("/api/shares", json=share_payload()).status_code == 201


def test_github_callback_rejects_unknown_user(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "exchange_code", lambda *a, **k: "someone-else")
    with make_client(
        tmp_path,
        github_client_id="id",
        github_client_secret="secret",
        github_allowed_users="huexpub",
    ) as client:
        started = client.get("/api/auth/github/start", follow_redirects=False)
        state = started.cookies.get(auth.STATE_COOKIE)
        response = client.get(
            f"/api/auth/github/callback?code=abc&state={state}", follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"].endswith("/login?error=not_allowed")
        assert client.post("/api/shares", json=share_payload()).status_code == 401


# ---------------------------------------------------------------- cookie 本身


def _forge(secret: bytes, subject: str, expires: int) -> str:
    token = base64.urlsafe_b64encode(subject.encode()).decode().rstrip("=")
    mac = base64.urlsafe_b64encode(
        hmac.new(secret, f"session:{expires}.{subject}".encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{expires}.{token}.{mac}"


def test_session_cookie_rejects_tampering(tmp_path):
    secret = b"k" * 32
    good = _forge(secret, "admin", int(time.time()) + 60)
    assert auth.session_subject(secret, good) == "admin"

    # 换身份但保留签名
    assert auth.session_subject(secret, good.replace(good.split(".")[1], "cm9vdA")) is None
    # 换密钥
    assert auth.session_subject(b"x" * 32, good) is None


def test_expired_session_cookie_is_rejected(tmp_path):
    secret = b"k" * 32
    stale = _forge(secret, "admin", int(time.time()) - 1)
    assert auth.session_subject(secret, stale) is None


@pytest.mark.parametrize("raw", ["", "garbage", "1.2", "a.b.c", "...", None])
def test_session_cookie_never_raises(raw):
    assert auth.session_subject(b"k" * 32, raw) is None
