"""粘贴流程的接口层：inspect 预览、识别落库、按形态渲染、文件嗅探。"""

from __future__ import annotations

import io

LOG = """2026-11-03 14:02:11 INFO  boot start
2026-11-03 14:02:12 WARN  retry flash
2026-11-03 14:02:14 ERROR timeout after 3000ms
"""

PYTHON = """def add(a, b):
    return a + b


class Box:
    def __init__(self):
        self.items = []
"""


def test_inspect_returns_kind_and_html_without_saving(client):
    response = client.post("/api/inspect", json={"text": LOG})
    assert response.status_code == 200
    body = response.json()
    assert body["detected"]["kind"] == "log"
    assert "log-row" in body["html"]
    # 不落库：这里拿不到 id，也不应该能按 id 取到东西
    assert "id" not in body


def test_inspect_force_kind(client):
    body = client.post(
        "/api/inspect", json={"text": PYTHON, "force": "text"}
    ).json()
    assert body["detected"]["kind"] == "text"
    assert "<pre" in body["html"]


def test_inspect_can_skip_render(client):
    body = client.post("/api/inspect", json={"text": LOG, "render": False}).json()
    assert body["detected"]["kind"] == "log"
    assert "html" not in body


def test_text_share_keeps_detected_kind(client):
    created = client.post(
        "/api/shares", json={"text": PYTHON, "max_reads": 2, "no_code": True}
    ).json()
    assert created["detected"]["kind"] == "code"
    assert created["detected"]["language"] == "python"

    meta = client.get(f"/api/shares/{created['id']}").json()
    assert meta["detected"]["kind"] == "code"


def test_render_endpoint_consumes_and_returns_html(client):
    created = client.post("/api/shares", json={"text": LOG, "max_reads": 1, "no_code": True}).json()
    share_id = created["id"]

    body = client.get(f"/api/shares/{share_id}/render").json()
    assert body["detected"]["kind"] == "log"
    assert "log-row" in body["html"]
    assert body["share"]["state"] == "burned"

    again = client.get(f"/api/shares/{share_id}/render")
    assert again.status_code == 410


def test_render_requires_extract_code(client):
    created = client.post(
        "/api/shares", json={"text": PYTHON, "extract_code": "4242", "max_reads": 5}
    ).json()
    share_id = created["id"]

    denied = client.get(f"/api/shares/{share_id}/render")
    assert denied.status_code == 403

    client.post(f"/api/shares/{share_id}/unlock", json={"code": "4242"})
    ok = client.get(f"/api/shares/{share_id}/render")
    assert ok.status_code == 200
    assert "code-block" in ok.json()["html"]


def test_render_rejects_file_share(client):
    created = client.post(
        "/api/shares",
        files={"file": ("a.bin", io.BytesIO(b"\x00\x01\x02"), "application/octet-stream")},
        data={"no_code": "1"},
    ).json()
    response = client.get(f"/api/shares/{created['id']}/render")
    assert response.status_code == 400


def test_text_file_is_detected_on_upload(client):
    created = client.post(
        "/api/shares",
        files={"file": ("boot.log", io.BytesIO(LOG.encode()), "text/plain")},
    ).json()
    assert created["detected"]["kind"] == "log"


def test_binary_file_is_not_detected_as_text(client):
    created = client.post(
        "/api/shares",
        files={"file": ("fw.bin", io.BytesIO(b"\x00\xff\x00\xfe" * 64), "application/octet-stream")},
    ).json()
    assert created["detected"]["kind"] == "binary"
    assert created["kind"] == "file"


def test_image_file_is_labelled_image(client):
    created = client.post(
        "/api/shares",
        files={"file": ("shot.png", io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00"), "image/png")},
    ).json()
    assert created["detected"]["kind"] == "image"
    assert created["detected"]["kind_label"] == "图片"


def test_share_page_has_no_links_out(client):
    """分享页是对外只读的：拿到链接的人不该被引到上传页或任何站内页面。

    「我也要传一个」以前放在焚毁页里，等于把内部工具的入口发给外部收件人。
    品牌标识也从 `<a href="/">` 改成了 `<span>`，所以这里断言的是「没有指向站内的
    锚点」，而不是「没有 `href=`」—— 静态资源本来就要有 href。
    """
    html = client.get("/s/anything").text
    assert "我也要传一个" not in html
    assert '<a class="brand"' not in html
    assert 'href="/"' not in html


def test_status_endpoint_reports_read_and_burn(client):
    """发送端历史靠它把「待读取」刷成「已读」。"""
    created = client.post(
        "/api/shares", json={"text": "hello", "max_reads": 2, "no_code": True}
    ).json()
    share_id = created["id"]

    fresh = client.post("/api/shares/status", json={"ids": [share_id]}).json()["items"][0]
    assert fresh["state"] == "active"
    assert fresh["read_count"] == 0
    assert fresh["remaining"] == 2

    client.get(f"/api/shares/{share_id}/content")
    after = client.post("/api/shares/status", json={"ids": [share_id]}).json()["items"][0]
    assert after["read_count"] == 1
    assert after["remaining"] == 1

    client.get(f"/api/shares/{share_id}/content")
    burned = client.post("/api/shares/status", json={"ids": [share_id]}).json()["items"][0]
    assert burned["state"] == "burned"
    assert burned["remaining"] == 0


def test_status_endpoint_does_not_consume(client):
    created = client.post("/api/shares", json={"text": "x", "max_reads": 1, "no_code": True}).json()
    for _ in range(3):
        client.post("/api/shares/status", json={"ids": [created["id"]]})
    meta = client.get(f"/api/shares/{created['id']}").json()
    assert meta["remaining"] == 1


def test_status_endpoint_handles_unknown_ids(client):
    items = client.post(
        "/api/shares/status", json={"ids": ["nope1", "nope2"]}
    ).json()["items"]
    assert [item["state"] for item in items] == ["deleted", "deleted"]


def test_status_never_leaks_content(client):
    created = client.post("/api/shares", json={"text": "secret body", "no_code": True}).json()
    body = client.post("/api/shares/status", json={"ids": [created["id"]]}).json()
    assert "secret body" not in str(body)
    assert set(body["items"][0]) == {"id", "state", "read_count", "remaining", "expires_at"}


# ------------------------------------------------- 两阶段：打开不扣，关闭才扣


def test_claim_does_not_consume_but_returns_content(client):
    created = client.post(
        "/api/shares", json={"text": "preview me", "max_reads": 2, "no_code": True}
    ).json()
    share_id = created["id"]

    claim = client.post(f"/api/shares/{share_id}/claim")
    assert claim.status_code == 200
    assert claim.text == "preview me"
    token = claim.headers["x-claim-token"]
    assert token

    # 关键：claim 之后次数没动
    meta = client.get(f"/api/shares/{share_id}").json()
    assert meta["read_count"] == 0
    assert meta["remaining"] == 2
    assert meta["state"] == "active"


def test_commit_consumes_one_read(client):
    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 2, "no_code": True}
    ).json()
    share_id = created["id"]
    token = client.post(f"/api/shares/{share_id}/claim").headers["x-claim-token"]

    view = client.post(f"/api/shares/{share_id}/commit", json={"claim_token": token}).json()
    assert view["read_count"] == 1
    assert view["remaining"] == 1
    assert view["state"] == "active"


def test_last_commit_burns(client, settings):
    created = client.post(
        "/api/shares",
        files={"file": ("a.bin", io.BytesIO(b"payload"), "application/octet-stream")},
        data={"max_reads": "1", "no_code": "1"},
    ).json()
    share_id = created["id"]
    blob = settings.blob_dir / share_id[:2] / share_id
    assert blob.exists()

    claim = client.post(f"/api/shares/{share_id}/claim")
    assert claim.content == b"payload"
    # 打开之后原件还在（还没 commit）
    assert blob.exists()

    view = client.post(
        f"/api/shares/{share_id}/commit", json={"claim_token": claim.headers["x-claim-token"]}
    ).json()
    assert view["state"] == "burned"
    assert not blob.exists()

    # 再来一次拿不到内容
    assert client.post(f"/api/shares/{share_id}/claim").status_code == 410


def test_commit_rejects_unknown_token(client):
    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 3, "no_code": True}
    ).json()
    share_id = created["id"]
    client.post(f"/api/shares/{share_id}/claim")

    bad = client.post(f"/api/shares/{share_id}/commit", json={"claim_token": "forged"})
    assert bad.status_code == 409
    assert bad.json()["error"] == "claim_expired"
    # 伪造的 commit 不能扣掉次数
    assert client.get(f"/api/shares/{share_id}").json()["read_count"] == 0


def test_claim_is_reused_not_double_counted(client):
    """同一个用户重开弹窗不该产生两个占位。"""
    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 1, "no_code": True}
    ).json()
    share_id = created["id"]
    first = client.post(f"/api/shares/{share_id}/claim").headers["x-claim-token"]
    second = client.post(f"/api/shares/{share_id}/claim").headers["x-claim-token"]
    assert first == second

    view = client.post(f"/api/shares/{share_id}/commit", json={"claim_token": first}).json()
    assert view["state"] == "burned"


def test_expired_claim_counts_as_consumed(client, settings):
    """只打开不关闭（浏览器崩了）：占位超时后算作已消费，不能无限白看。"""
    import time as _time

    from bfs.db import session

    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 1, "no_code": True}
    ).json()
    share_id = created["id"]
    client.post(f"/api/shares/{share_id}/claim")

    with session(settings.db_path) as conn:
        conn.execute(
            "UPDATE shares SET claim_expires_at = ? WHERE id = ?",
            (int(_time.time()) - 1, share_id),
        )
        conn.commit()

    # 下一次访问会把超时的占位结算掉（次数用尽 -> 焚毁）
    assert client.post(f"/api/shares/{share_id}/claim").status_code == 410
    assert client.get(f"/api/shares/{share_id}").json()["state"] == "burned"


def test_commit_is_idempotent(client):
    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 5, "no_code": True}
    ).json()
    share_id = created["id"]
    token = client.post(f"/api/shares/{share_id}/claim").headers["x-claim-token"]
    first = client.post(f"/api/shares/{share_id}/commit", json={"claim_token": token}).json()
    # 第二次（重复的关闭事件）：token 已清空，应当被拒，但不应再扣一次
    second = client.post(f"/api/shares/{share_id}/commit", json={"claim_token": token})
    assert second.status_code == 409
    assert client.get(f"/api/shares/{share_id}").json()["read_count"] == first["read_count"]


def test_commit_accepts_token_in_query_string(client):
    """浏览器关闭页面时用 sendBeacon：它只能带字符串，token 只能走查询串。"""
    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 1, "no_code": True}
    ).json()
    share_id = created["id"]
    token = client.post(f"/api/shares/{share_id}/claim").headers["x-claim-token"]

    # 不带 body，只有查询串 —— 这正是 sendBeacon 能表达的形状
    view = client.post(f"/api/shares/{share_id}/commit?claim_token={token}").json()
    assert view["state"] == "burned"
    assert view["read_count"] == 1


def test_commit_without_token_is_rejected(client):
    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 3, "no_code": True}
    ).json()
    response = client.post(f"/api/shares/{created['id']}/commit")
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_sharing_page_is_minimal(client):
    """验证码之后只剩内容和一个动作：不要弹窗，也不要元信息面板。"""
    html = client.get("/s/anything").text
    assert 'id="viewerMask"' not in html
    assert 'id="gateMask"' in html
    assert 'id="viewer"' in html
    # 接收端不显示这些只有发送者才关心的东西
    for noise in ('id="metaKv"', 'id="remainHint"', 'id="burnWarn"', 'sha256'):
        assert noise not in html


# ---------------------------------------------------------------- 文件预览


def test_inspect_trusts_markdown_extension(client):
    """`.md` 文件即使正文没有任何 Markdown 语法，也该按 Markdown 渲染。

    接收端把文件读成字符串后借 `/inspect` 渲染，所以这里必须能把文件名传进去。
    """
    plain = "只是一段普通的话，没有标题也没有列表。"
    without_name = client.post("/api/inspect", json={"text": plain}).json()
    assert without_name["detected"]["kind"] == "text"

    with_name = client.post(
        "/api/inspect", json={"text": plain, "filename": "notes.md"}
    ).json()
    assert with_name["detected"]["kind"] == "markdown"
    assert '<div class="prose">' in with_name["html"]


def test_inspect_trusts_dotfile_style_extensions(client):
    for name, expected in [
        ("a.md", "markdown"),
        ("a.markdown", "markdown"),
        ("a.csv", "table"),
        ("a.diff", "diff"),
        ("a.log", "log"),
        ("a.json", "json"),
    ]:
        body = client.post(
            "/api/inspect", json={"text": "x", "filename": name}
        ).json()
        assert body["detected"]["kind"] == expected, name


def test_text_file_is_previewable_not_only_downloadable(client):
    """文本类文件上传后应带 detected.kind，接收端据此内联预览而不是只给下载。"""
    created = client.post(
        "/api/shares",
        files={"file": ("notes.md", io.BytesIO("# hi\n\n- a\n".encode()), "text/markdown")},
        data={"no_code": "1"},
    ).json()
    assert created["detected"]["kind"] == "markdown"
    assert created["kind"] == "file"
def test_claim_requires_extract_code(client):
    created = client.post(
        "/api/shares", json={"text": "x", "max_reads": 2, "extract_code": "4242"}
    ).json()
    share_id = created["id"]

    denied = client.post(f"/api/shares/{share_id}/claim")
    assert denied.status_code == 403
    assert denied.json()["error"] == "bad_code"

    client.post(f"/api/shares/{share_id}/unlock", json={"code": "4242"})
    ok = client.post(f"/api/shares/{share_id}/claim")
    assert ok.status_code == 200
    assert ok.text == "x"
