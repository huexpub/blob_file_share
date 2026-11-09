"""部署形态：默认提取码、挂载前缀、对外域名。

这一组测的是「同一份代码怎么挂到不同 URL 下」，以及默认安全姿态 ——
它们是部署时最容易出错、也最难在本地发现的部分。
"""

from __future__ import annotations

import io

import pytest

from bfs.app import create_app
from bfs.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        rate_limit_per_minute=1000,
        audit=False,
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------- 默认提取码


def test_extract_code_is_generated_by_default(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/shares", json={"text": "hi"}).json()
        code = created["extract_code"]
        assert code and len(code) == 4 and code.isdigit()
        assert created["has_extract_code"] is True

        # 没码拿不到内容
        assert client.get(f"/api/shares/{created['id']}/content").status_code == 403
        # 带码可以
        assert (
            client.get(f"/api/shares/{created['id']}/content", params={"code": code}).text
            == "hi"
        )


def test_default_code_can_be_disabled(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path, auto_extract_code=False)
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/shares", json={"text": "hi"}).json()
        assert created["extract_code"] is None
        assert created["has_extract_code"] is False
        assert client.get(f"/api/shares/{created['id']}/content").text == "hi"


def test_no_code_flag_wins_over_auto(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/shares", json={"text": "hi", "no_code": True}
        ).json()
        assert created["extract_code"] is None
        assert client.get(f"/api/shares/{created['id']}/content").text == "hi"


def test_user_supplied_code_is_kept(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/shares", json={"text": "hi", "extract_code": "8765"}
        ).json()
        assert created["extract_code"] == "8765"


def test_files_get_auto_code_too(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/shares",
            files={"file": ("a.bin", io.BytesIO(b"data"), "application/octet-stream")},
        ).json()
        assert created["extract_code"] and created["has_extract_code"] is True


# ---------------------------------------------------------------- 挂载前缀


def test_prefix_moves_everything_under_it(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path, path_prefix="bfs")
    with TestClient(create_app(settings)) as client:
        # 页面、静态资源、API、分享页都在 /bfs 下
        assert client.get("/bfs/").status_code == 200
        assert client.get("/bfs/static/app.js").status_code == 200
        assert client.get("/bfs/api/limits").status_code == 200

        created = client.post("/bfs/api/shares", json={"text": "hi", "no_code": True}).json()
        # 分享链接也用前缀，而不是 /s/；且前缀只出现一次
        assert created["url"].endswith(f"/bfs/{created['id']}")
        assert created["url"].count("/bfs") == 1
        assert client.get(f"/bfs/{created['id']}").status_code == 200

    # 根路径不再提供服务（避免和主站抢路由）
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/limits").status_code == 404


def test_prefix_share_segment_and_ota(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path, path_prefix="bfs")
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/bfs/api/shares",
            files={"file": ("fw.bin", io.BytesIO(b"\x00\x01fw"), "application/octet-stream")},
            data={"no_code": "1"},
        ).json()
        assert created["url"].endswith(f"/bfs/{created['id']}")

        ota = client.post(
            f"/bfs/api/shares/{created['id']}/ota",
            json={"manage_token": created["manage_token"]},
        ).json()
        assert ota["url"].endswith(f"/bfs/ota/{ota['token']}")
        assert ota["url"].count("/bfs") == 1
        assert client.get(f"/bfs/ota/{ota['token']}").content == b"\x00\x01fw"


def test_explicit_share_segment(tmp_path):
    from fastapi.testclient import TestClient

    # 想保留 /s/<id> 而不是 /bfs/<id> 时，显式指定 share_segment=s
    settings = make_settings(tmp_path, path_prefix="bfs", share_segment="s")
    with TestClient(create_app(settings)) as client:
        created = client.post("/bfs/api/shares", json={"text": "hi", "no_code": True}).json()
        assert created["url"].endswith(f"/bfs/s/{created['id']}")
        assert created["url"].count("/bfs") == 1  # 不能重复拼前缀
        assert client.get(f"/bfs/s/{created['id']}").status_code == 200


def test_page_injects_mount_info(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path, path_prefix="bfs")
    with TestClient(create_app(settings)) as client:
        html = client.get("/bfs/").text
        assert '__BFS_BASE__="/bfs"' in html
        assert '__BFS_API_PREFIX__="/bfs/api"' in html


def test_page_injects_root_mount_info(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        html = client.get("/").text
        assert '__BFS_BASE__=""' in html
        assert '__BFS_SHARE_PREFIX__="/s"' in html
        assert '__BFS_API_PREFIX__="/api"' in html


# ---------------------------------------------------------------- 对外域名


def test_base_url_overrides_request_origin(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path, base_url="https://view.lotenall.com/bfs", path_prefix="bfs")
    with TestClient(create_app(settings)) as client:
        created = client.post("/bfs/api/shares", json={"text": "hi", "no_code": True}).json()
        assert created["url"] == f"https://view.lotenall.com/bfs/{created['id']}"


def test_url_falls_back_to_request_origin(tmp_path):
    from fastapi.testclient import TestClient

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, )) as client:
        created = client.post("/api/shares", json={"text": "hi", "no_code": True}).json()
        assert created["url"].startswith("http://testserver/s/")


# ---------------------------------------------------------------- 派生属性


@pytest.mark.parametrize(
    "path_prefix,share_segment,expected_prefix,expected_share,expected_api",
    [
        ("", "", "", "/s", "/api"),
        ("bfs", "", "/bfs", "/bfs", "/bfs/api"),
        ("/bfs/", "", "/bfs", "/bfs", "/bfs/api"),
        ("bfs", "s", "/bfs", "/bfs/s", "/bfs/api"),
        ("", "d", "", "/d", "/api"),
        ("a/b", "", "/a/b", "/a/b", "/a/b/api"),
    ],
)
def test_derived_paths(path_prefix, share_segment, expected_prefix, expected_share, expected_api):
    settings = Settings(
        data_dir=__import__("pathlib").Path("."),
        path_prefix=path_prefix,
        share_segment=share_segment,
    )
    assert settings.prefix == expected_prefix
    assert settings.share_prefix == expected_share
    assert settings.api_prefix == expected_api


# ---------------------------------------------------------------- 页面结构
#
# 页脚必须留在 `.shell` 里面。`.shell` 是 `min-height: 100vh` 的 flex 列，
# 页脚在它外面时，内容不足一屏会把页脚顶到折叠线以下 —— 表现就是
# 「内容明明很短，却要滚一下才看得到页脚」。这类问题只有真机才看得见，
# 所以用结构断言盯住它。


def _footer_is_inside_shell(page: str) -> bool:
    import re

    index = page.find('class="foot"')
    assert index > 0, "页面里找不到页脚"
    head = page[:index]
    # 页脚出现时还没闭合的 div 层数：1 = 在 .shell 里，0 = 已经在 .shell 外面
    return len(re.findall(r"<div\b", head)) - len(re.findall(r"</div>", head)) == 1


@pytest.mark.parametrize("path", ["/"])
def test_upload_page_footer_is_inside_shell(tmp_path, path):
    from fastapi.testclient import TestClient

    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        page = client.get(path).text
    assert _footer_is_inside_shell(page)


def test_share_page_footer_is_inside_shell(tmp_path):
    from fastapi.testclient import TestClient

    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post("/api/shares", json={"text": "x", "kind": "text"})
        share = created.json()
        page = client.get(f"/s/{share['id']}").text
    assert _footer_is_inside_shell(page)
    # 页脚只能出现一次：曾经 share.html 里同时有正确的和外部的各一份
    assert page.count('class="foot"') == 1


# ---------------------------------------------------------------- 页脚自定义
#
# 备案号这类信息属于部署配置，不该硬编码在 5 个 HTML 里。
# 由 BFS_FOOTER_HTML 注入，允许 HTML（要放 <a href>）。

BEIAN = (
    '<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">'
    "\u7ca4ICP\u590712345678\u53f7-1</a>"
)


def test_footer_defaults_when_unset(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(create_app(make_settings(tmp_path))) as client:
        page = client.get("/").text
    assert "BLOB FILE SHARING @ 2026" in page
    assert "__FOOTER__" not in page


def test_footer_html_is_injected_on_every_page(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(create_app(make_settings(tmp_path, footer_html=BEIAN))) as client:
        created = client.post("/api/shares", json={"text": "x", "kind": "text"}).json()
        paths = ["/", "/login", "/history", "/settings", f"/s/{created['id']}"]
        for path in paths:
            page = client.get(path).text
            assert "beian.miit.gov.cn" in page, path
            assert "__FOOTER__" not in page, path
            # 默认文案被替换掉，不是叠加
            assert "BLOB FILE SHARING @ 2026" not in page, path


def test_footer_link_is_not_escaped(tmp_path):
    """备案号要能点：注入的 <a> 必须原样输出，不能被转义成文本。"""
    from fastapi.testclient import TestClient

    with TestClient(create_app(make_settings(tmp_path, footer_html=BEIAN))) as client:
        page = client.get("/").text
    assert "<a href=\"https://beian.miit.gov.cn/\"" in page
    assert "&lt;a href" not in page
