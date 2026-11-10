"""User-owned share history and its shared CI section."""

from __future__ import annotations

from typing import Any

from .db import session, transaction
from .service import Service, ShareError

SHARED_OWNER = "shared"


def record_owner(service: Service, share_id: str, owner: str | None) -> None:
    if owner is None:
        return
    with transaction(service.settings.db_path) as conn:
        conn.execute("UPDATE shares SET owner_id = ? WHERE id = ?", (owner, share_id))


def list_shares(service: Service, owners: str | list[str], base_url: str = "") -> list[dict[str, Any]]:
    owner_list = [owners] if isinstance(owners, str) else list(dict.fromkeys(owners))
    if not owner_list:
        return []
    placeholders = ",".join("?" for _ in owner_list)
    with session(service.settings.db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM shares WHERE owner_id IN ({placeholders}) ORDER BY created_at DESC LIMIT 500",
            owner_list,
        ).fetchall()
    return [service._view(row, base_url) for row in rows]


def clear_owned(service: Service, owners: str | list[str], ip: str | None = None) -> int:
    """Clear the selected history by burning every still-live item."""
    owner_list = [owners] if isinstance(owners, str) else list(dict.fromkeys(owners))
    if not owner_list:
        return 0
    placeholders = ",".join("?" for _ in owner_list)
    with transaction(service.settings.db_path) as conn:
        # 连已经焚毁的一起清掉：清空的意义就是「列表回到零」
        rows = conn.execute(
            f"SELECT * FROM shares WHERE owner_id IN ({placeholders})",
            owner_list,
        ).fetchall()
        for row in rows:
            if row["burned_at"] is None:
                service._burn(conn, row, "history_clear", ip)
            service._delete_row(conn, row["id"])
    return len(rows)


def status_items(service: Service, owners: str | list[str] | None, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    params: list[Any] = list(ids)
    owner_filter = ""
    if owners is not None:
        owner_list = [owners] if isinstance(owners, str) else list(dict.fromkeys(owners))
        if not owner_list:
            return []
        owner_marks = ",".join("?" for _ in owner_list)
        owner_filter = f" AND owner_id IN ({owner_marks})"
        params.extend(owner_list)
    with session(service.settings.db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM shares WHERE id IN ({placeholders}){owner_filter}",
            params,
        ).fetchall()
        found = {row["id"]: row for row in rows}
        items = []
        for share_id in ids:
            row = found.get(share_id)
            if row is None:
                items.append({"id": share_id, "state": "deleted", "read_count": 0, "remaining": 0})
                continue
            items.append({
                "id": share_id,
                "state": service._state(row),
                "read_count": row["read_count"],
                "remaining": service._remaining(row),
                "expires_at": row["expires_at"],
            })
    return items


def burn_owned(service: Service, owners: str | list[str], share_id: str, ip: str | None = None) -> dict[str, Any]:
    """焚毁并**移除**这条记录。

    管理模式下「删除」用户期望的是这条东西彻底消失 —— 保留一条
    「已焚毁」的墓碑只会让列表越来越长，而且对方打开链接看到的仍然是
    「已焚毁」，没有任何区别。所以：先焚毁（删文件、清正文），再删行。

    墓碑机制本身没坏，它服务于「阅后即焚」这类被动销毁：那时接收方
    需要一个明确的「已消失」页面。主动删除不需要。
    """
    owner_list = [owners] if isinstance(owners, str) else list(dict.fromkeys(owners))
    if not owner_list:
        raise ShareError("not_found", "分享不存在或不属于当前账户", 404)
    placeholders = ",".join("?" for _ in owner_list)
    with transaction(service.settings.db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM shares WHERE id = ? AND owner_id IN ({placeholders})",
            [share_id, *owner_list],
        ).fetchone()
        if row is None:
            raise ShareError("not_found", "分享不存在或不属于当前账户", 404)
        if row["burned_at"] is None:
            service._burn(conn, row, "manual", ip)
        service._delete_row(conn, share_id)
        snap = service._view(row)
    snap["state"] = "deleted"
    return snap
