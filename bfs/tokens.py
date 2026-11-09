"""CI 上传用的 API token。

设计边界（这是整个文件存在的理由）
--------------------------------
token **不是一个全局开关**，而是**某个 OAuth 用户的凭据**：

  * 只有 ``oauth_enabled`` 的实例才存在这张表（纯密码/公开模式没有用户概念）；
  * 只有登录用户能为**自己**创建 token，管理员也不能替别人发；
  * 用 token 上传的内容，归属人就是 token 的所有者 ——
    后台历史里看到的就是「我 CI 传的」，而不是一堆无主孤儿。

存储与校验
----------
明文只在创建那一刻返回一次，库里只留 SHA-256。这跟密码不是一回事：
密码要防「库泄露后还原」，所以用 pbkdf2 慢哈希；token 本身是
``secrets.token_urlsafe(32)`` 的高熵随机串（256 bit），暴力枚举不是威胁模型，
所以确定性哈希就够 —— 而且必须确定性，否则每次校验都得全表扫。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from .db import session, transaction
from .service import Service, ShareError

#: 展示用的前缀，方便用户认出这是 BFS 的 token
TOKEN_PREFIX = "bfs_"
#: 一个用户最多同时持有几个（够 CI 分环境用，又不至于失控）
MAX_TOKENS_PER_OWNER = 10


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(service: Service, owner: str, label: str | None = None) -> dict[str, Any]:
    """新建 token。返回明文 —— 调用方必须立刻展示，之后再也拿不到。"""
    if not owner or owner == "anonymous":
        raise ShareError("forbidden", "需要登录后才能创建 token", 403)

    with transaction(service.settings.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM api_tokens WHERE owner_id = ?", (owner,)
        ).fetchone()["n"]
        if count >= MAX_TOKENS_PER_OWNER:
            raise ShareError(
                "too_many_tokens",
                f"每个人最多 {MAX_TOKENS_PER_OWNER} 个 token，请先撤销不用的",
                409,
            )
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_tokens (token_hash, owner_id, label, created_at) VALUES (?,?,?,?)",
            (_digest(token), owner, (label or "").strip() or None, _now()),
        )
        service._audit(conn, "token_create", None, None, owner)
    return {"token": token, "owner": owner, "label": label or ""}


def list_tokens(service: Service, owner: str) -> list[dict[str, Any]]:
    """列自己的 token。只回元信息，永远不回明文（库里也没有）。"""
    with session(service.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT label, created_at, last_used_at FROM api_tokens"
            " WHERE owner_id = ? ORDER BY created_at DESC",
            (owner,),
        ).fetchall()
    return [dict(row) for row in rows]


def revoke_tokens(service: Service, owner: str, label: str | None = None) -> int:
    """撤销 token。``label=None`` 撤销该用户全部。"""
    with transaction(service.settings.db_path) as conn:
        if label is None:
            cursor = conn.execute("DELETE FROM api_tokens WHERE owner_id = ?", (owner,))
        else:
            cursor = conn.execute(
                "DELETE FROM api_tokens WHERE owner_id = ? AND label = ?", (owner, label)
            )
        service._audit(conn, "token_revoke", None, None, owner)
        return cursor.rowcount


def resolve_owner(service: Service, token: str) -> str | None:
    """校验 Bearer token，返回归属用户；无效返回 ``None``。"""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    digest = _digest(token)
    with transaction(service.settings.db_path) as conn:
        row = conn.execute(
            "SELECT owner_id FROM api_tokens WHERE token_hash = ?", (digest,)
        ).fetchone()
        if row is None:
            return None
        # 记录最近使用时间：用户靠它判断哪个 token 还在被 CI 用
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?",
            (_now(), digest),
        )
    return row["owner_id"]


def _now() -> int:
    import time

    return int(time.time())
