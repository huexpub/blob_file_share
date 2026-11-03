"""提取码、解锁票据、短 ID。

* 提取码：pbkdf2-hmac-sha256 + 每份分享独立的 salt。4 位数字本身强度很低，
  真正的防线是接口层的失败次数限速（见 ``ratelimit``），哈希只是不让
  数据库泄露后一眼看出提取码。
* 解锁票据：``expires.signature``，签名覆盖 share id + 过期时间 + 服务密钥，
  不需要为「已解锁」再建一张表。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from pathlib import Path

# 去掉容易混淆的 0/O/1/l/i，念给同事听也不会错。
ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
ID_LENGTH = 10

_PBKDF2_ROUNDS = 60_000


def new_share_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


def new_manage_token() -> str:
    return secrets.token_urlsafe(24)


def new_ota_token() -> str:
    return secrets.token_urlsafe(24)


def random_extract_code(digits: int = 4) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))


def hash_extract_code(code: str) -> tuple[str, str]:
    """返回 (hash, salt)，都是十六进制字符串。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return digest.hex(), salt


def verify_extract_code(code: str, expected_hash: str | None, salt: str | None) -> bool:
    if not expected_hash or not salt:
        return True
    digest = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return hmac.compare_digest(digest.hex(), expected_hash)


def load_secret(path: Path) -> bytes:
    """服务密钥：首次启动生成并落盘，重启后已发出的解锁 Cookie 仍然有效。"""
    if path.exists():
        data = path.read_bytes().strip()
        if data:
            return data
    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secret.hex().encode("ascii"))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret.hex().encode("ascii")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def issue_ticket(secret: bytes, share_id: str, ttl_seconds: int) -> str:
    expires = int(time.time()) + ttl_seconds
    payload = f"{share_id}.{expires}"
    mac = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{expires}.{_b64(mac)}"


def verify_ticket(secret: bytes, share_id: str, ticket: str | None) -> bool:
    if not ticket or "." not in ticket:
        return False
    expires_raw, mac = ticket.split(".", 1)
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    payload = f"{share_id}.{expires}"
    expected = _b64(hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest())
    return hmac.compare_digest(expected, mac)


def cookie_name(share_id: str) -> str:
    return f"bfs_unlock_{share_id}"
