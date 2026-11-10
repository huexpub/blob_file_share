"""OAuth 管理模式：CI token、按用户归属的历史、历史删除即焚毁。

这批测试的核心是几条**边界**，而不是个别实现细节：

  * 没启用 OAuth 时，token 相关端点全部 403（不存在「用户」就没有凭据）；
  * CI token 上传的内容归属 token 所有者，不是无主数据；
  * 别人的历史看不到、也烧不掉；
  * 历史删除 = 立即焚毁（内容真的从磁盘消失）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bfs import auth
from bfs.app import create_app
from bfs.config import Settings

OAUTH_ON = dict(
    gitea_url="https://git.example.test",
    gitea_client_id="cid",
    gitea_client_secret="csecret",
)


def client_for(tmp_path, **kwargs) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                data_dir=tmp_path / "data",
                rate_limit_per_minute=1000,
                audit=False,
                **kwargs,
            )
        )
    )


def as_user(client: TestClient, subject: str = "gitea:alice") -> None:
    """直接下发一张有效会话 cookie，跳过真实 OAuth 往返。"""
    client.cookies.set(
        auth.SESSION_COOKIE,
        auth.issue_session(client.app.state.secret, subject, 3600),
        path="/",
    )


# ---------------------------------------------------------------- OAuth 闸门


def test_tokens_are_unavailable_without_oauth(tmp_path):
    """公开模式 / 纯密码模式都没有「用户」，所以 token 端点一律 403。"""
    for extra in ({}, {"admin_password": "p"}):
        with client_for(tmp_path, **extra) as client:
            if extra:
                as_user(client, "admin")
            assert client.get("/api/tokens").status_code == 403
            assert client.post("/api/tokens", json={"label": "ci"}).status_code == 403
            assert client.get("/api/shares/mine").status_code == 403


def test_batch_upload_rejected_without_oauth(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/shares/batch",
            headers={"Authorization": "Bearer bfs_whatever"},
            files=[("file", ("a.bin", b"a"))],
        )
        assert response.status_code == 403
        assert response.json()["error"] == "oauth_required"


def test_password_gate_still_works_but_has_no_history(tmp_path):
    """没启用 OAuth 时行为完全不变：能发、能用，只是没有账户历史。"""
    with client_for(tmp_path, admin_password="p") as client:
        client.post("/api/auth/login", json={"username": "", "password": "p"})
        assert client.post("/api/shares", json={"text": "hi", "no_code": True}).status_code == 201


# ---------------------------------------------------------------- token 生命周期


def test_token_is_returned_once_and_never_listed(tmp_path):
    with client_for(tmp_path, **OAUTH_ON) as client:
        as_user(client)
        created = client.post("/api/tokens", json={"label": "ci-firmware"})
        assert created.status_code == 201
        payload = created.json()
        assert payload["token"].startswith("bfs_")
        assert payload["owner"] == "gitea:alice"

        listed = client.get("/api/tokens").json()
        assert [item["label"] for item in listed["items"]] == ["ci-firmware"]
        # 明文只在创建响应里出现一次，列表里绝不回显
        assert "token" not in listed["items"][0]
        assert payload["token"] not in str(listed)


def test_token_can_be_revoked(tmp_path):
    with client_for(tmp_path, **OAUTH_ON) as client:
        as_user(client)
        token = client.post("/api/tokens", json={"label": "ci"}).json()["token"]

        def upload():
            return client.post(
                "/api/shares/batch",
                headers={"Authorization": f"Bearer {token}"},
                files=[("file", ("a.bin", b"payload"))],
            )

        assert upload().status_code == 201
        assert client.delete("/api/tokens").json()["revoked"] == 1
        assert upload().status_code == 401


def test_token_limits_per_owner(tmp_path):
    with client_for(tmp_path, **OAUTH_ON) as client:
        as_user(client)
        for index in range(10):
            assert client.post("/api/tokens", json={"label": f"t{index}"}).status_code == 201
        blocked = client.post("/api/tokens", json={"label": "over"})
        assert blocked.status_code == 409
        assert blocked.json()["error"] == "too_many_tokens"


# ---------------------------------------------------------------- CI 上传


def test_batch_upload_attributes_content_to_token_owner(tmp_path):
    with client_for(tmp_path, **OAUTH_ON, api_default_ttl_hours=0) as client:
        as_user(client)
        token = client.post("/api/tokens", json={"label": "ci"}).json()["token"]

        response = client.post(
            "/api/shares/batch",
            headers={"Authorization": f"Bearer {token}"},
            files=[
                ("file", ("firmware.bin", b"one")),
                ("file", ("symbols.zip", b"two")),
            ],
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["owner"] == "gitea:alice"
        # 一次上传 = 一条分享：两个文件共享同一个链接和提取码
        share = payload["share"]
        assert share["file_count"] == 2
        assert [f["file_name"] for f in share["files"]] == ["firmware.bin", "symbols.zip"]
        assert "2 个文件" in payload["message"]
        assert share["url"] in payload["message"]
        # 默认永久 + 不限次数：构建产物是给人长期下载的
        assert share["expires_at"] is None
        assert share["max_reads"] == -1

        history = client.get("/api/shares/mine").json()
        assert history["owner"] == "gitea:alice"
        assert [item["id"] for item in history["items"]] == [share["id"]]


def test_batch_upload_with_unknown_token_is_401(tmp_path):
    with client_for(tmp_path, **OAUTH_ON) as client:
        response = client.post(
            "/api/shares/batch",
            headers={"Authorization": "Bearer bfs_not-a-real-token"},
            files=[("file", ("a.bin", b"a"))],
        )
        assert response.status_code == 401


# ---------------------------------------------------------------- 历史隔离与焚毁


def test_history_is_per_user(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", rate_limit_per_minute=1000, audit=False, **OAUTH_ON
    )
    app = create_app(settings)
    alice, bob = TestClient(app), TestClient(app)
    try:
        as_user(alice, "gitea:alice")
        as_user(bob, "gitea:bob")
        mine = alice.post("/api/shares", json={"text": "alice data", "no_code": True}).json()

        assert bob.get("/api/shares/mine").json()["items"] == []
        # 知道 ID 也不能烧别人的
        assert bob.delete(f"/api/shares/{mine['id']}").status_code == 404
        # 状态查询同样不越权
        leaked = bob.post("/api/shares/status", json={"ids": [mine["id"]]}).json()["items"][0]
        assert leaked["state"] == "deleted"
    finally:
        alice.close()
        bob.close()


def test_delete_history_burns_content_and_drops_the_row(tmp_path):
    """管理模式下「删除」= 内容销毁 + 记录消失，不留墓碑。

    墓碑是给「阅后即焚」用的：接收方需要看到「已焚毁」。
    主动删除不需要，留着只会让历史列表越来越长。
    """
    with client_for(tmp_path, **OAUTH_ON) as client:
        as_user(client)
        share = client.post(
            "/api/shares",
            json={"text": "sensitive body", "no_code": True},
        ).json()

        deleted = client.delete(f"/api/shares/{share['id']}")
        assert deleted.status_code == 200

        # 记录从列表里消失
        assert client.get("/api/shares/mine").json()["items"] == []
        # 内容真的没了：链接和正文都取不到
        assert client.get(f"/api/shares/{share['id']}").status_code == 404
        assert client.get(f"/api/shares/{share['id']}/content").status_code == 404


def test_clear_history_empties_the_list(tmp_path):
    with client_for(tmp_path, **OAUTH_ON) as client:
        as_user(client)
        for text in ("one", "two", "three"):
            client.post("/api/shares", json={"text": text, "no_code": True})

        assert client.delete("/api/shares/mine").json()["burned"] == 3
        # 清空的意义就是列表回到零，不是全变成「已焚毁」
        assert client.get("/api/shares/mine").json()["items"] == []


def test_clear_history_also_drops_previous_tombstones(tmp_path):
    """已经焚毁（墓碑）的记录也要一起清掉，否则列表永远清不干净。"""
    with client_for(tmp_path, **OAUTH_ON) as client:
        as_user(client)
        share = client.post("/api/shares", json={"text": "x", "no_code": True}).json()
        # 让它变成墓碑：读满一次（默认 max_reads=1）会焚毁但不删行
        client.get(f"/api/shares/{share['id']}/content?code=")
        assert client.get("/api/shares/mine").json()["items"][0]["state"] == "burned"

        client.delete("/api/shares/mine")
        assert client.get("/api/shares/mine").json()["items"] == []


# ---------------------------------------------------------------- Gitea OAuth


def test_gitea_callback_creates_session_and_history(tmp_path, monkeypatch):
    with client_for(tmp_path, **OAUTH_ON) as client:
        start = client.get("/api/auth/gitea/start", follow_redirects=False)
        assert start.status_code == 302
        assert start.headers["location"].startswith(
            "https://git.example.test/login/oauth/authorize?"
        )
        state = start.cookies[auth.STATE_COOKIE]
        monkeypatch.setattr(auth, "exchange_gitea_code", lambda *a, **k: "dev-user")

        callback = client.get(
            f"/api/auth/gitea/callback?code=oauth-code&state={state}",
            follow_redirects=False,
        )
        assert callback.status_code == 302
        assert client.get("/api/auth/session").json()["authenticated"] is True

        share = client.post("/api/shares", json={"text": "from gitea", "no_code": True}).json()
        history = client.get("/api/shares/mine").json()
        assert history["owner"] == "gitea:dev-user"
        assert history["items"][0]["id"] == share["id"]


def test_gitea_requires_three_settings(tmp_path):
    with client_for(tmp_path, gitea_url="https://git.example.test") as client:
        assert client.get("/api/auth/gitea/start").status_code == 400
        assert client.get("/api/auth/session").json()["methods"]["gitea"] is False
