"""一次发布 = 一条分享。

这是产品语义的回归测试：用户可能同时粘文本 + 拖两个文件，
那必须是**一条**链接、一个提取码、一次焚毁，而不是 N 条分享。
"""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from bfs.app import create_app
from bfs.config import Settings


def make_client(tmp_path, **overrides) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data", rate_limit_per_minute=1000, audit=False, **overrides
    )
    return TestClient(create_app(settings))


def test_two_files_make_one_share(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/shares",
            files=[
                ("file", ("a.bin", b"aaa", "application/octet-stream")),
                ("file", ("b.txt", b"bbb", "text/plain")),
            ],
            data={"no_code": "1"},
        )
        assert response.status_code == 201
        share = response.json()
        # 一条分享，不是两条
        assert share["file_count"] == 2
        assert [f["file_name"] for f in share["files"]] == ["a.bin", "b.txt"]
        assert share["size"] == 6


def test_text_and_files_share_one_link(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/shares",
            files=[("file", ("firmware.bin", b"\x00\x01", "application/octet-stream"))],
            data={"text": "def hello():\n    return 1", "no_code": "1"},
        )
        assert response.status_code == 201
        share = response.json()
        assert share["file_count"] == 1
        # 有文本时形态以文本为主
        assert share["detected"]["kind"] == "code"
        assert share["files"][0]["file_name"] == "firmware.bin"


def test_multi_file_claim_returns_manifest(tmp_path):
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/shares",
            files=[
                ("file", ("one.txt", b"first", "text/plain")),
                ("file", ("two.txt", b"second", "text/plain")),
            ],
            data={"no_code": "1"},
        ).json()
        share_id = created["id"]

        claim = client.post(f"/api/shares/{share_id}/claim")
        assert claim.status_code == 200
        manifest = claim.json()
        assert manifest["manifest"] is True
        assert len(manifest["files"]) == 2
        token = claim.headers["x-claim-token"]

        # 逐个取文件：不消耗次数，只认 claim_token
        first = client.get(f"/api/shares/{share_id}/file/0?claim_token={token}")
        assert first.status_code == 200
        assert first.content == b"first"
        second = client.get(f"/api/shares/{share_id}/file/1?claim_token={token}")
        assert second.content == b"second"

        # 这份分享没有提取码，所以无票据也能取（与单文件下载的语义一致）
        assert client.get(f"/api/shares/{share_id}/file/0").status_code == 200


def test_multi_file_requires_code_without_ticket(tmp_path):
    """有提取码的多文件分享：没有票据就取不到单个文件。"""
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/shares",
            files=[
                ("file", ("one.txt", b"first", "text/plain")),
                ("file", ("two.txt", b"second", "text/plain")),
            ],
            data={"extract_code": "9876"},
        ).json()
        share_id = created["id"]
        assert created["extract_code"] == "9876"

        denied = client.get(f"/api/shares/{share_id}/file/0")
        assert denied.status_code == 403

        unlock = client.post(f"/api/shares/{share_id}/unlock", json={"code": "9876"})
        assert unlock.status_code == 200
        claim = client.post(f"/api/shares/{share_id}/claim")
        token = claim.headers["x-claim-token"]
        ok = client.get(f"/api/shares/{share_id}/file/0?claim_token={token}")
        assert ok.status_code == 200
        assert ok.content == b"first"


def test_multi_file_download_is_a_zip(tmp_path):
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/shares",
            files=[
                ("file", ("one.txt", b"first", "text/plain")),
                ("file", ("two.txt", b"second", "text/plain")),
            ],
            data={"no_code": "1", "max_reads": "5"},
        ).json()

        download = client.get(f"/api/shares/{created['id']}/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert sorted(archive.namelist()) == ["one.txt", "two.txt"]
            assert archive.read("one.txt") == b"first"


def test_burning_removes_every_file_on_disk(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", rate_limit_per_minute=1000, audit=False)
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/shares",
            files=[
                ("file", ("one.bin", b"1", "application/octet-stream")),
                ("file", ("two.bin", b"2", "application/octet-stream")),
            ],
            data={"no_code": "1"},
        ).json()

        folder = settings.blob_dir / created["id"][:2]
        before = sorted(p.name for p in folder.iterdir() if p.name.startswith(created["id"]))
        assert len(before) == 2

        burned = client.post(
            f"/api/shares/{created['id']}/burn",
            json={"manage_token": created["manage_token"]},
        )
        assert burned.status_code == 200
        after = sorted(p.name for p in folder.iterdir() if p.name.startswith(created["id"]))
        assert after == []
        assert client.get(f"/api/shares/{created['id']}/download").status_code == 410


def test_single_file_keeps_old_behaviour(tmp_path):
    """单文件仍然直接流回文件本身，不套 zip —— 兼容脚本和 OTA。"""
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/shares",
            files={"file": ("a.bin", b"payload", "application/octet-stream")},
            data={"no_code": "1"},
        ).json()
        assert created["file_count"] == 1
        download = client.get(f"/api/shares/{created['id']}/download")
        assert download.status_code == 200
        assert download.content == b"payload"


def test_text_plus_one_file_keeps_the_text(tmp_path):
    """文本 + 单个文件：不能因为「只有一个文件」就把文本吞掉。

    回归测试。之前 _extract_payload 只要 len(files)==1 就直接返回文件，
    于是分享页只显示文件内容，粘的那段文本消失了。
    """
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/shares",
            files=[("file", ("data.txt", b"file-body", "text/plain"))],
            data={"text": "the pasted text", "no_code": "1", "max_reads": "-1"},
        ).json()
        assert created["file_count"] == 1

        claim = client.post(f"/api/shares/{created['id']}/claim")
        manifest = claim.json()
        # 走清单而不是裸文件，文本才带得出来
        assert manifest["manifest"] is True
        assert manifest["text"] == "the pasted text"
        assert [f["file_name"] for f in manifest["files"]] == ["data.txt"]


def test_pure_single_file_still_streams_raw(tmp_path):
    """纯单文件仍然直接流回文件本身（脚本、OTA 依赖这个行为）。"""
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/shares",
            files=[("file", ("only.bin", b"raw-bytes", "application/octet-stream"))],
            data={"no_code": "1", "max_reads": "-1"},
        ).json()
        claim = client.post(f"/api/shares/{created['id']}/claim")
        assert claim.headers["content-type"].startswith("application/octet-stream")
        assert claim.content == b"raw-bytes"

