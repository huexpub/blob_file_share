"""核心行为：建分享、阅后即焚、提取码、直链、清理。"""

from __future__ import annotations

import io

from bfs import security
from bfs.service import Service, ShareError


def test_create_text_and_burn_after_one_read(client):
    response = client.post(
        "/api/shares",
        json={"text": "hello world", "ttl_hours": 1, "max_reads": 1, "no_code": True},
    )
    assert response.status_code == 201
    created = response.json()
    assert created["kind"] == "text"
    assert created["remaining"] == 1
    assert created["url"].endswith(f"/s/{created['id']}")

    share_id = created["id"]
    content = client.get(f"/api/shares/{share_id}/content")
    assert content.status_code == 200
    assert content.text == "hello world"

    # 第二次：已焚毁
    again = client.get(f"/api/shares/{share_id}/content")
    assert again.status_code == 410
    assert again.json()["error"] == "burned"

    meta = client.get(f"/api/shares/{share_id}").json()
    assert meta["state"] == "burned"
    assert meta["remaining"] == 0


def test_file_roundtrip_keeps_digests(client):
    payload = bytes(range(256)) * 4
    response = client.post(
        "/api/shares",
        files={"file": ("firmware.bin", io.BytesIO(payload), "application/octet-stream")},
        data={"ttl_hours": "1", "max_reads": "1", "no_code": "1"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["size"] == len(payload)
    assert created["file_name"] == "firmware.bin"

    meta = client.get(f"/api/shares/{created['id']}").json()
    assert meta["sha256"] == created["sha256"]

    download = client.get(f"/api/shares/{created['id']}/download")
    assert download.status_code == 200
    assert download.content == payload
    assert 'filename="firmware.bin"' in download.headers["content-disposition"]
    assert download.headers["x-share-state"] == "burned"

    assert client.get(f"/api/shares/{created['id']}/download").status_code == 410


def test_last_download_deletes_blob_file(client, settings):
    payload = b"abc" * 1000
    created = client.post(
        "/api/shares",
        files={"file": ("x.bin", io.BytesIO(payload), "application/octet-stream")},
        data={"max_reads": "1", "no_code": "1"},
    ).json()
    blob = settings.blob_dir / created["id"][:2] / created["id"]
    assert blob.exists()

    assert client.get(f"/api/shares/{created['id']}/download").content == payload
    # 最后一次读之后原件必须真的消失（不是只把数据库字段清掉）
    assert not blob.exists()
    # 传送带上的临时副本不能留在 tmp/
    assert [p for p in settings.tmp_dir.glob("send-*")] == []


def test_extract_code_gate_and_unlock_cookie(client):
    created = client.post(
        "/api/shares",
        json={"text": "secret", "extract_code": "1234", "max_reads": 2},
    ).json()
    share_id = created["id"]
    assert created["has_extract_code"] is True
    assert created["extract_code"] == "1234"

    # 没码 -> 403
    denied = client.get(f"/api/shares/{share_id}/content")
    assert denied.status_code == 403
    assert denied.json()["error"] == "bad_code"

    # 解锁（不扣次数）
    unlocked = client.post(f"/api/shares/{share_id}/unlock", json={"code": "1234"})
    assert unlocked.status_code == 200
    assert unlocked.json()["read_count"] == 0

    # 解锁后可以直接取内容，不再需要 code 参数
    content = client.get(f"/api/shares/{share_id}/content")
    assert content.status_code == 200
    assert content.text == "secret"
    assert client.get(f"/api/shares/{share_id}").json()["remaining"] == 1


def test_wrong_code_is_counted_then_rate_limited(client, app):
    created = client.post(
        "/api/shares", json={"text": "x", "extract_code": "9999"}
    ).json()
    share_id = created["id"]
    # 服务端默认每个 IP 每份分享每分钟 10 次
    statuses = [
        client.post(f"/api/shares/{share_id}/unlock", json={"code": "0000"}).status_code
        for _ in range(12)
    ]
    assert 403 in statuses
    assert 429 in statuses


def test_unlimited_reads_stay_alive(client):
    created = client.post(
        "/api/shares", json={"text": "keep me", "max_reads": -1, "no_code": True}
    ).json()
    share_id = created["id"]
    for _ in range(3):
        assert client.get(f"/api/shares/{share_id}/content").status_code == 200
    meta = client.get(f"/api/shares/{share_id}").json()
    assert meta["remaining"] is None
    assert meta["state"] == "active"


def test_peek_does_not_consume(client):
    created = client.post("/api/shares", json={"text": "peek", "max_reads": 1, "no_code": True}).json()
    for _ in range(5):
        assert client.get(f"/api/shares/{created['id']}").status_code == 200
    assert client.get(f"/api/shares/{created['id']}").json()["remaining"] == 1


def test_ota_token_one_shot(client):
    payload = b"\x01\x02firmware"
    created = client.post(
        "/api/shares",
        files={"file": ("app.bin", io.BytesIO(payload), "application/octet-stream")},
        data={"max_reads": "1", "no_code": "1"},
    ).json()

    ota = client.post(
        f"/api/shares/{created['id']}/ota",
        json={"manage_token": created["manage_token"], "label": "esp32", "max_uses": 1},
    )
    assert ota.status_code == 200, ota.text
    token = ota.json()["token"]

    head = client.head(f"/ota/{token}")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(payload))

    pull = client.get(f"/ota/{token}")
    assert pull.status_code == 200
    assert pull.content == payload
    assert pull.headers["x-share-sha256"] == created["sha256"]

    assert client.get(f"/ota/{token}").status_code == 410


def test_ota_requires_manage_token(client):
    created = client.post("/api/shares", json={"text": "x", "no_code": True}).json()
    denied = client.post(
        f"/api/shares/{created['id']}/ota", json={"manage_token": "wrong"}
    )
    assert denied.status_code == 403


def test_manual_burn_removes_blob(client, settings):
    created = client.post(
        "/api/shares",
        files={"file": ("a.bin", io.BytesIO(b"x" * 1024), "application/octet-stream")},
        data={"no_code": "1"},
    ).json()
    blob = settings.blob_dir / created["id"][:2] / created["id"]
    assert blob.exists()

    burned = client.post(
        f"/api/shares/{created['id']}/burn",
        json={"manage_token": created["manage_token"]},
    )
    assert burned.status_code == 200
    assert burned.json()["state"] == "burned"
    assert not blob.exists()


def test_purge_drops_expired_tombstones(settings):
    service = Service(settings)
    created = service.create_text("hello", ttl_hours=1)
    share_id = created.share["id"]
    assert settings.db_path.exists()

    # 手动把过期时间放到过去，模拟时间流逝
    import time

    from bfs.db import session

    with session(settings.db_path) as conn:
        conn.execute(
            "UPDATE shares SET expires_at = ? WHERE id = ?",
            (int(time.time()) - 10, share_id),
        )
        conn.commit()

    result = service.purge()
    assert result["burned"] == 1

    # 墓碑超过保留期才删行
    with session(settings.db_path) as conn:
        conn.execute(
            "UPDATE shares SET burned_at = ? WHERE id = ?",
            (int(time.time()) - settings.tombstone_hours * 3600 - 10, share_id),
        )
        conn.commit()
    result = service.purge()
    assert result["removed"] == 1
    with session(settings.db_path) as conn:
        row = conn.execute("SELECT 1 FROM shares WHERE id = ?", (share_id,)).fetchone()
    assert row is None


def test_expired_share_is_gone(client, settings):
    created = client.post("/api/shares", json={"text": "old", "no_code": True}).json()
    import time

    from bfs.db import session

    with session(settings.db_path) as conn:
        conn.execute(
            "UPDATE shares SET expires_at = ? WHERE id = ?",
            (int(time.time()) - 1, created["id"]),
        )
        conn.commit()

    response = client.get(f"/api/shares/{created['id']}/content")
    assert response.status_code == 410
    assert response.json()["error"] == "expired"


def test_unknown_share_is_404(client):
    assert client.get("/api/shares/nope").status_code == 404


def test_text_size_limit_enforced(settings):
    service = Service(settings)
    try:
        service.create_text("x" * (settings.max_text_bytes + 1))
    except ShareError as exc:
        assert exc.code == "too_large"
        assert exc.status == 413
    else:  # pragma: no cover
        raise AssertionError("超过上限时应抛 ShareError")


def test_ticket_roundtrip():
    secret = b"k" * 32
    ticket = security.issue_ticket(secret, "abc", 60)
    assert security.verify_ticket(secret, "abc", ticket)
    assert not security.verify_ticket(secret, "other", ticket)
    assert not security.verify_ticket(secret, "abc", "garbage")
    assert not security.verify_ticket(secret, "abc", security.issue_ticket(secret, "abc", -5))
