"""唯一的业务写入口。

所有改状态的动作都从这里走（API 只做参数校验和 HTTP 映射），
这样「还剩几次」「什么时候该焚毁」只有一套判定。

核心状态机：

    创建 ──► 存活（read_count < max_reads 且未过期）
                │
                │  打开/下载 一次（read_count += 1）
                ├─► 还有次数 …… 继续存活
                └─► 次数用尽 / 已过期 ──► 焚毁（删 blob、清正文、记 burned_at）

焚毁只做一次：``_burn`` 是幂等的，已焚毁的行不会重复删文件。

对外暴露的 ``ShareView`` 是**脱敏后**的字典：永远不含 text_content / blob 路径，
只有 ``open_share`` / ``download`` / OTA 三个出口才会真的把内容读出来。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import security
from .analyze import KIND_LABELS, TEXT_SNIFF_BYTES, analyze, analyze_file
from .config import Settings
from .db import init_db, session, transaction

TEXT_HEAD_BYTES = TEXT_SNIFF_BYTES


class ShareError(Exception):
    """业务错误。``code`` 同时作为 API 的 ``error`` 字段与前端提示键。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(slots=True)
class Created:
    share: dict[str, Any]
    extract_code: str | None
    manage_token: str


@dataclass(slots=True)
class FilePayload:
    """待发送的文件。

    为什么不在响应里直接用 blob 原路径：最后一次读取会**立刻焚毁**原始文件，
    而响应体是稍后才流出去的。所以先做一个硬链接（同一卷上 O(1)）当「传送带」，
    原文件随后可以安全删除；发送结束后 API 再把传送带摘掉。
    """

    path: Path
    file_name: str
    content_type: str
    size: int


def _now() -> int:
    return int(time.time())


def _expires_at(ttl_hours: int) -> int | None:
    if ttl_hours <= 0:
        return None
    return _now() + ttl_hours * 3600


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _digests(data: bytes) -> tuple[str, str, str]:
    return (
        hashlib.sha256(data).hexdigest(),
        hashlib.md5(data).hexdigest(),  # noqa: S324 —— 只是给固件做校验和，不当密码学用途
        f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
    )


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"}


def _binary_kind(file_name: str | None, content_type: str | None) -> str:
    """文本嗅探失败时的兜底形态：至少把图片和普通文件分开。"""
    type_ = (content_type or "").lower()
    if type_.startswith("image/"):
        return "image"
    if file_name:
        suffix = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if suffix in _IMAGE_SUFFIXES:
            return "image"
    return "binary"


class Service:
    def __init__(self, settings: Settings, secret: bytes | None = None) -> None:
        self.settings = settings
        settings.ensure_dirs()
        init_db(settings.db_path)
        self.secret = secret or security.load_secret(settings.secret_path)

    # ---------------------------------------------------------------- 内部

    def _audit(
        self,
        conn,
        kind: str,
        share_id: str | None,
        ip: str | None,
        detail: str | None = None,
    ) -> None:
        if not self.settings.audit:
            return
        conn.execute(
            "INSERT INTO events (at, share_id, kind, ip, detail) VALUES (?,?,?,?,?)",
            (_now(), share_id, kind, ip, detail),
        )

    def _blob_path(self, relative: str) -> Path:
        # 只允许 data_dir/blob_dir 下的相对路径；数据库被改坏也不会读到系统文件
        root = self.settings.blob_dir.resolve()
        path = (self.settings.blob_dir / relative).resolve()
        if root not in path.parents:
            raise ShareError("bad_blob", "存储路径异常", 500)
        return path

    def _burn(self, conn, row, reason: str, ip: str | None = None) -> None:
        """幂等焚毁：删文件、清正文、打墓碑。"""
        if row["burned_at"] is not None:
            return
        if row["blob"]:
            try:
                self._blob_path(row["blob"]).unlink(missing_ok=True)
            except OSError:
                pass
        conn.execute(
            "UPDATE shares SET text_content = NULL, blob = NULL, burned_at = ?,"
            " claim_token = NULL, claim_expires_at = NULL WHERE id = ?",
            (_now(), row["id"]),
        )
        conn.execute("DELETE FROM ota_tokens WHERE share_id = ?", (row["id"],))
        self._audit(conn, "burn", row["id"], ip, reason)

    @staticmethod
    def _remaining(row) -> int | None:
        """剩余可读次数；None = 不限。"""
        if row["max_reads"] < 0:
            return None
        return max(0, row["max_reads"] - row["read_count"])

    def _state(self, row) -> str:
        if row["burned_at"] is not None:
            return "burned"
        expires_at = row["expires_at"]
        if expires_at is not None and expires_at <= _now():
            return "expired"
        if row["max_reads"] >= 0 and row["read_count"] >= row["max_reads"]:
            return "exhausted"
        return "active"

    def _view(self, row, base_url: str = "") -> dict[str, Any]:
        state = self._state(row)
        kind = row["detected_kind"] or ("text" if row["kind"] == "text" else "binary")
        language = row["language"]
        view = {
            "id": row["id"],
            "kind": row["kind"],
            "file_name": row["file_name"],
            "content_type": row["content_type"],
            "size": row["size"],
            "sha256": row["sha256"],
            "md5": row["md5"],
            "crc32": row["crc32"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "max_reads": row["max_reads"],
            "read_count": row["read_count"],
            "remaining": self._remaining(row),
            "state": state,
            "has_extract_code": row["extract_hash"] is not None,
            "markdown": bool(row["markdown"]),
            "detected": {
                "kind": kind,
                "kind_label": KIND_LABELS.get(kind, kind),
                "language": language,
            },
            "url": self.settings.public_url(base_url, row["id"]) if base_url else "",
        }
        return view

    def _get_row(self, conn, share_id: str):
        row = conn.execute("SELECT * FROM shares WHERE id = ?", (share_id,)).fetchone()
        if row is None:
            raise ShareError("not_found", "链接不存在或已被焚毁", 404)
        return row

    def burn_if_expired(self, conn, row) -> bool:
        """清理任务用：过期就地焚毁。返回是否真的烧了。"""
        if row["burned_at"] is None and self._state(row) == "expired":
            self._burn(conn, row, "expired")
            return True
        return False

    # ---------------------------------------------------------------- 创建

    def create_text(
        self,
        text: str,
        *,
        ttl_hours: int | None = None,
        max_reads: int | None = None,
        extract_code: str | None = None,
        no_code: bool = False,
        markdown: bool = False,
        kind: str | None = None,
        ip: str | None = None,
        base_url: str = "",
        owner_id: str | None = None,
    ) -> Created:
        payload = text.encode("utf-8")
        if not payload:
            raise ShareError("empty", "内容不能为空")
        if len(payload) > self.settings.max_text_bytes:
            raise ShareError(
                "too_large",
                f"文本超过 {self.settings.max_text_bytes // 1024} KB 上限",
                413,
            )
        # 识别在服务端做一次、随分享存下来：分享页就能用和上传页一样的形态渲染，
        # 不会出现「发出去之后变成了另一个样子」。
        analysis = analyze(text, force=kind)
        if analysis.kind == "markdown":
            markdown = True
        if no_code:
            extract_code, _generated = None, False
        else:
            extract_code, _generated = self._resolve_extract_code(extract_code)
        ttl = self._resolve_ttl(ttl_hours)
        reads = self._resolve_reads(max_reads)
        code_hash = code_salt = None
        if extract_code:
            code_hash, code_salt = security.hash_extract_code(extract_code)
        share_id = self._new_id()
        token = security.new_manage_token()
        sha, md5, crc = _digests(payload)
        with transaction(self.settings.db_path) as conn:
            conn.execute(
                """INSERT INTO shares
                   (id, kind, file_name, content_type, size, sha256, md5, crc32, blob,
                    text_content, markdown, detected_kind, language, extract_hash,
                    extract_salt, manage_token, created_at, expires_at, max_reads,
                    read_count, owner_id)
                   VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (
                    share_id,
                    "text",
                    None,
                    "text/plain; charset=utf-8",
                    len(payload),
                    sha,
                    md5,
                    crc,
                    text,
                    1 if markdown else 0,
                    analysis.kind,
                    analysis.language,
                    code_hash,
                    code_salt,
                    token,
                    _now(),
                    ttl,
                    reads,
                    owner_id,
                ),
            )
            self._audit(conn, "create_text", share_id, ip, f"{len(payload)}B {analysis.kind}")
            row = self._get_row(conn, share_id)
        view = self._view(row, base_url)
        view["detected"].update(analysis.to_dict())
        return Created(view, extract_code, token)

    def create_file(
        self,
        *,
        file_name: str,
        data: Iterable[bytes] | bytes,
        content_type: str,
        ttl_hours: int | None = None,
        max_reads: int | None = None,
        extract_code: str | None = None,
        no_code: bool = False,
        ip: str | None = None,
        base_url: str = "",
        owner_id: str | None = None,
    ) -> Created:
        """流式写盘：边写边算摘要，不把整个文件读进内存。

        同时嗅探前 256 KB：如果它是文本（.log / .md / .py / .csv …），
        就把识别结果一起存下来 —— 分享页于是能按「日志 / 表格 / 代码」渲染，
        而不是甩一个下载按钮。二进制（.bin 固件）不嗅探成功，照旧走下载。
        """
        ttl = self._resolve_ttl(ttl_hours)
        reads = self._resolve_reads(max_reads)
        if no_code:
            extract_code = None
        else:
            extract_code, _generated = self._resolve_extract_code(extract_code)
        share_id = self._new_id()
        token = security.new_manage_token()
        relative = f"{share_id[:2]}/{share_id}"
        target = self._blob_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)

        sha = hashlib.sha256()
        md5 = hashlib.md5()  # noqa: S324 —— 固件校验和
        crc = 0
        size = 0
        head = bytearray()
        limit = self.settings.max_file_bytes
        chunks = [data] if isinstance(data, (bytes, bytearray)) else data
        try:
            with target.open("wb") as fh:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise ShareError(
                            "too_large",
                            f"文件超过 {limit // (1024 * 1024)} MB 上限",
                            413,
                        )
                    if len(head) < TEXT_HEAD_BYTES:
                        head.extend(chunk[: TEXT_HEAD_BYTES - len(head)])
                    sha.update(chunk)
                    md5.update(chunk)
                    crc = zlib.crc32(chunk, crc)
                    fh.write(chunk)
            if size == 0:
                raise ShareError("empty", "文件是空的")

            analysis = analyze_file(bytes(head), file_name, content_type)
            detected_kind = analysis.kind if analysis else _binary_kind(file_name, content_type)
            language = analysis.language if analysis else None

            code_hash = code_salt = None
            if extract_code:
                code_hash, code_salt = security.hash_extract_code(extract_code)
            with transaction(self.settings.db_path) as conn:
                conn.execute(
                    """INSERT INTO shares
                       (id, kind, file_name, content_type, size, sha256, md5, crc32,
                        blob, text_content, markdown, detected_kind, language,
                        extract_hash, extract_salt, manage_token, created_at,
                        expires_at, max_reads, read_count, owner_id)
                       VALUES (?,?,?,?,?,?,?,?,?,NULL,0,?,?,?,?,?,?,?,?,0,?)""",
                    (
                        share_id,
                        "file",
                        file_name or "download.bin",
                        content_type or "application/octet-stream",
                        size,
                        sha.hexdigest(),
                        md5.hexdigest(),
                        f"{crc & 0xFFFFFFFF:08x}",
                        relative,
                        detected_kind,
                        language,
                        code_hash,
                        code_salt,
                        token,
                        _now(),
                        ttl,
                        reads,
                        owner_id,
                    ),
                )
                self._audit(conn, "create_file", share_id, ip, f"{size}B {detected_kind or 'bin'}")
                row = self._get_row(conn, share_id)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        view = self._view(row, base_url)
        if analysis is not None:
            view["detected"].update(analysis.to_dict())
        return Created(view, extract_code, token)

    def _resolve_extract_code(self, code: str | None) -> tuple[str | None, bool]:
        """决定这份分享的提取码。

        * 用户填了 -> 用他的
        * 没填 且 服务端开了自动 -> 随机生成 4 位（返回值第二项为 True）
        * 没填 且 关了自动 -> 不设提取码

        为什么默认自动生成：发送端最怕的是「忘了加码」——
        链接一旦被转发出去就收不回来了。默认给一个，成本几乎为零。
        """
        if code:
            return code.strip(), False
        if self.settings.auto_extract_code:
            return security.random_extract_code(), True
        return None, False

    def _resolve_ttl(self, ttl_hours: int | None) -> int | None:
        # 允许 ttl=0 表示「不设过期」，但也要给上限，否则就成了永久链接
        hours = self.settings.default_ttl_hours if ttl_hours is None else ttl_hours
        hours = _clamp(hours, 0, self.settings.max_ttl_hours)
        return _expires_at(hours)

    def _resolve_reads(self, max_reads: int | None) -> int:
        reads = self.settings.default_max_reads if max_reads is None else max_reads
        if reads < 0:
            return -1
        return _clamp(reads, 1, self.settings.max_reads_limit)

    def _new_id(self) -> str:
        # 碰撞概率极低，但插入前还是查一次，免得主键冲突报 500
        for _ in range(8):
            candidate = security.new_share_id()
            with session(self.settings.db_path) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM shares WHERE id = ?", (candidate,)
                ).fetchone()
            if not exists:
                return candidate
        raise ShareError("id_exhausted", "生成短 ID 失败，请重试", 500)

    # ---------------------------------------------------------------- 读取

    def peek(self, share_id: str, base_url: str = "") -> dict[str, Any]:
        """只读元数据，不消耗次数。分享页首屏用它。"""
        with session(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)
        return self._view(row, base_url)

    # ---------------------------------------------------- 两阶段读取（预览 -> 销毁）
    #
    # 接收端的语义是「关掉弹窗才算消费」，但内容必须先送到浏览器才能看。
    # 于是拆成两步：
    #
    #   claim  取内容并占位，**不扣次数、不删盘**，返回一个 claim_token
    #   commit 用户关闭弹窗时调，这时才扣次数；次数用尽才真正删盘
    #
    # 只 claim 不 commit 的情况由 ``claim_expires_at`` 兜底：超过时限后
    # 任何一次访问都会把这次占位算成已消费，避免「打开一次就无限白看」。
    # 这个超时也顺带覆盖「浏览器崩了、没来得及 commit」。

    CLAIM_TTL_SECONDS = 180

    def claim(
        self,
        share_id: str,
        *,
        code: str | None = None,
        verified: bool = False,
        ip: str | None = None,
        base_url: str = "",
    ) -> tuple[dict[str, Any], str | FilePayload | None, str]:
        """预览：取内容但不扣次数。返回 ``(view, payload, claim_token)``。"""
        # 先单独结算「超时未关闭」的占位并提交。
        # 不能和下面的读取放同一个事务里：一旦后面 raise（410/403），
        # 回滚会把这次结算也一起撤掉，超时占位就永远不会生效。
        with transaction(self.settings.db_path) as conn:
            self._expire_claim_if_needed(conn, self._get_row(conn, share_id), ip)

        with transaction(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)

            state = self._state(row)
            if state != "active":
                raise ShareError(_state_error(state), _state_message(state), 410)

            if (
                not verified
                and row["extract_hash"]
                and not security.verify_extract_code(
                    code or "", row["extract_hash"], row["extract_salt"]
                )
            ):
                self._audit(conn, "unlock_failed", share_id, ip)
                raise ShareError("bad_code", "提取码不正确", 403)

            token = row["claim_token"]
            # 已经有未过期的占位（同一个用户重开弹窗）：复用，不重复占位
            if not token:
                token = secrets.token_urlsafe(16)
                conn.execute(
                    "UPDATE shares SET claim_token = ?, claim_expires_at = ? WHERE id = ?",
                    (token, _now() + self.CLAIM_TTL_SECONDS, share_id),
                )
            self._audit(conn, "claim", share_id, ip)
            row = self._get_row(conn, share_id)

            payload = self._extract_payload(row)
            view = self._view(row, base_url)
            view["claimed"] = True
            view["claim_token"] = token
        return view, payload, token

    def commit(self, share_id: str, claim_token: str, ip: str | None = None) -> dict[str, Any]:
        """真正消费：扣次数，次数用尽就删盘。关闭弹窗时调。"""
        with transaction(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)

            if row["burned_at"] is not None:
                # 已经烧过了（占位超时被回收）：幂等返回，前端照样切成残骸
                return self._view(row)

            if not row["claim_token"] or not hmac_eq(row["claim_token"], claim_token):
                raise ShareError("claim_expired", "这次打开的凭证已失效", 409)

            read_count = row["read_count"] + 1
            exhausted = row["max_reads"] >= 0 and read_count >= row["max_reads"]
            conn.execute(
                "UPDATE shares SET read_count = ?, last_read_at = ?, claim_token = NULL,"
                " claim_expires_at = NULL WHERE id = ?",
                (read_count, _now(), share_id),
            )
            self._audit(
                conn, "read" if row["kind"] == "text" else "download", share_id, ip
            )
            row = self._get_row(conn, share_id)
            if exhausted:
                self._burn(conn, row, "exhausted", ip)
                row = self._get_row(conn, share_id)
            return self._view(row)

    def _expire_claim_if_needed(self, conn, row, ip: str | None = None) -> None:
        """占位超时 = 那次预览当作已消费（防止只打开不关闭就无限白看）。"""
        if row["burned_at"] is not None:
            return
        expires_at = row["claim_expires_at"]
        if not row["claim_token"] or expires_at is None or expires_at > _now():
            return
        read_count = row["read_count"] + 1
        exhausted = row["max_reads"] >= 0 and read_count >= row["max_reads"]
        conn.execute(
            "UPDATE shares SET read_count = ?, last_read_at = ?, claim_token = NULL,"
            " claim_expires_at = NULL WHERE id = ?",
            (read_count, _now(), row["id"]),
        )
        self._audit(conn, "claim_timeout", row["id"], ip)
        if exhausted or self._state(self._get_row(conn, row["id"])) == "exhausted":
            self._burn(conn, self._get_row(conn, row["id"]), "exhausted", ip)

    def _extract_payload(self, row) -> str | FilePayload | None:
        """把正文取出来（文件走传送带），供响应使用。"""
        if row["kind"] == "file":
            if not row["blob"]:
                raise ShareError("gone", "内容已被焚毁", 410)
            source = self._blob_path(row["blob"])
            if not source.exists():
                raise ShareError("gone", "内容已被焚毁", 410)
            return FilePayload(
                path=self._stage(source),
                file_name=row["file_name"] or "download.bin",
                content_type=row["content_type"],
                size=row["size"],
            )
        return row["text_content"]

    def open_share(
        self,
        share_id: str,
        *,
        code: str | None = None,
        consume: bool = True,
        verified: bool = False,
        ip: str | None = None,
        base_url: str = "",
    ) -> tuple[dict[str, Any], str | FilePayload | None]:
        """打开（查看 / 下载）并消耗一次。

        返回 ``(view, payload)``；``payload`` 是文本，或一个已经「取出来」的文件。
        ``consume=False`` 只验证提取码、不扣次数，用于解锁。
        ``verified=True`` 表示调用方已经用解锁票据证明过身份，跳过提取码校验。
        """
        with session(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)
        state = self._state(row)
        if state == "expired":
            # 「打开过期链接」要告诉对方是过期，而不是含糊的「已焚毁」。
            # 焚毁必须单独提交，否则下面 raise 会把事务回滚、文件留在盘上。
            with transaction(self.settings.db_path) as conn:
                self._burn(conn, self._get_row(conn, share_id), "expired", ip)
            raise ShareError("expired", "链接已过期", 410)
        if state != "active":
            raise ShareError(_state_error(state), _state_message(state), 410)

        with transaction(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)
            if row["burned_at"] is not None or self._state(row) != "active":
                # 两个请求同时到最后一次时，先到的那个已经烧掉了
                raise ShareError(_state_error(self._state(row)), _state_message(self._state(row)), 410)

            if (
                not verified
                and row["extract_hash"]
                and not security.verify_extract_code(
                    code or "", row["extract_hash"], row["extract_salt"]
                )
            ):
                self._audit(conn, "unlock_failed", share_id, ip)
                raise ShareError("bad_code", "提取码不正确", 403)

            text: str | None = row["text_content"]
            if row["kind"] == "file":
                if not row["blob"]:
                    raise ShareError("gone", "内容已被焚毁", 410)
                source = self._blob_path(row["blob"])
                if not source.exists():
                    raise ShareError("gone", "内容已被焚毁", 410)
                staged = self._stage(source)
                payload: str | FilePayload | None = FilePayload(
                    path=staged,
                    file_name=row["file_name"] or "download.bin",
                    content_type=row["content_type"],
                    size=row["size"],
                )
            else:
                payload = text

            if consume:
                read_count = row["read_count"] + 1
                exhausted = row["max_reads"] >= 0 and read_count >= row["max_reads"]
                conn.execute(
                    "UPDATE shares SET read_count = ?, last_read_at = ? WHERE id = ?",
                    (read_count, _now(), share_id),
                )
                self._audit(
                    conn, "read" if row["kind"] == "text" else "download", share_id, ip
                )
                row = self._get_row(conn, share_id)
                if exhausted:
                    # 次数用尽：立刻焚毁原文件；传送带上的硬链接让本次响应照常发完
                    self._burn(conn, row, "exhausted", ip)
                    row = self._get_row(conn, share_id)
            view = self._view(row, base_url)

        return view, payload

    def _stage(self, source: Path) -> Path:
        """给即将发出的文件造一个临时副本（优先硬链接，跨卷时退回复制）。"""
        staged = self.settings.tmp_dir / f"send-{secrets.token_hex(8)}"
        staged.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, staged)
        except OSError:
            shutil.copyfile(source, staged)
        return staged

    def release(self, payload: FilePayload | None) -> None:
        """响应发完后摘掉传送带。"""
        if isinstance(payload, FilePayload):
            payload.path.unlink(missing_ok=True)

    def read_blob(self, relative: str) -> Path:
        path = self._blob_path(relative)
        if not path.exists():
            raise ShareError("gone", "内容已被焚毁", 410)
        return path

    # ---------------------------------------------------------------- 管理

    def burn(self, share_id: str, manage_token: str, ip: str | None = None) -> dict[str, Any]:
        with transaction(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)
            if not hmac_eq(row["manage_token"], manage_token):
                raise ShareError("forbidden", "管理凭证无效", 403)
            self._burn(conn, row, "manual", ip)
            row = self._get_row(conn, share_id)
        return self._view(row)

    def stats(self, share_id: str, manage_token: str) -> dict[str, Any]:
        with session(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)
            if not hmac_eq(row["manage_token"], manage_token):
                raise ShareError("forbidden", "管理凭证无效", 403)
            otas = [
                dict(r)
                for r in conn.execute(
                    "SELECT token, label, max_uses, uses, created_at, expires_at, last_used_at"
                    " FROM ota_tokens WHERE share_id = ? ORDER BY created_at",
                    (share_id,),
                )
            ]
        return {"share": self._view(row), "ota_tokens": otas}

    # ---------------------------------------------------------------- OTA

    def create_ota(
        self,
        share_id: str,
        manage_token: str,
        *,
        label: str | None = None,
        max_uses: int | None = None,
        ttl_hours: int | None = None,
        ip: str | None = None,
        base_url: str = "",
    ) -> dict[str, Any]:
        token = security.new_ota_token()
        uses = self.settings.ota_default_uses if max_uses is None else max_uses
        uses = _clamp(uses, 1, self.settings.max_reads_limit)
        hours = self.settings.ota_ttl_hours if ttl_hours is None else ttl_hours
        hours = _clamp(hours, 0, self.settings.max_ttl_hours)
        expires = _expires_at(hours)
        with transaction(self.settings.db_path) as conn:
            row = self._get_row(conn, share_id)
            if not hmac_eq(row["manage_token"], manage_token):
                raise ShareError("forbidden", "管理凭证无效", 403)
            if self._state(row) != "active":
                raise ShareError("gone", "分享已不可用，无法创建直链", 410)
            conn.execute(
                """INSERT INTO ota_tokens
                   (token, share_id, label, max_uses, uses, created_at, expires_at)
                   VALUES (?,?,?,?,0,?,?)""",
                (token, share_id, label, uses, _now(), expires),
            )
            self._audit(conn, "create_ota", share_id, ip, label)
        root = (self.settings.base_url or base_url).rstrip("/")
        return {
            "token": token,
            "label": label,
            "max_uses": uses,
            "uses": 0,
            "expires_at": expires,
            "url": f"{root}{self.settings.prefix}/ota/{token}",
        }

    def peek_ota(self, token: str) -> dict[str, Any]:
        """HEAD 探活：只回答「这个直链还能用吗、有多大」，不扣次数。"""
        with session(self.settings.db_path) as conn:
            ota = conn.execute("SELECT * FROM ota_tokens WHERE token = ?", (token,)).fetchone()
            if ota is None:
                raise ShareError("not_found", "直链不存在", 404)
            if ota["expires_at"] is not None and ota["expires_at"] <= _now():
                raise ShareError("ota_expired", "直链已过期", 410)
            if ota["uses"] >= ota["max_uses"]:
                raise ShareError("ota_exhausted", "直链使用次数已用尽", 410)
            row = self._get_row(conn, ota["share_id"])
            if self._state(row) != "active":
                raise ShareError("gone", "对应的分享已焚毁", 410)
            view = self._view(row)
        view["ota_uses"] = ota["uses"]
        view["ota_max_uses"] = ota["max_uses"]
        return view

    def consume_ota(self, token: str, ip: str | None = None) -> tuple[dict[str, Any], FilePayload]:
        """设备侧拉取：校验 token，扣一次使用额度，返回待发送的文件。"""
        with transaction(self.settings.db_path) as conn:
            ota = conn.execute("SELECT * FROM ota_tokens WHERE token = ?", (token,)).fetchone()
            if ota is None:
                raise ShareError("not_found", "直链不存在", 404)
            if ota["expires_at"] is not None and ota["expires_at"] <= _now():
                raise ShareError("ota_expired", "直链已过期", 410)
            if ota["uses"] >= ota["max_uses"]:
                raise ShareError("ota_exhausted", "直链使用次数已用尽", 410)

            row = self._get_row(conn, ota["share_id"])
            if self._state(row) != "active":
                raise ShareError("gone", "对应的分享已焚毁", 410)

            conn.execute(
                "UPDATE ota_tokens SET uses = uses + 1, last_used_at = ? WHERE token = ?",
                (_now(), token),
            )
            self._audit(conn, "ota_pull", ota["share_id"], ip, ota["label"])

            if row["blob"]:
                staged = self._stage(self.read_blob(row["blob"]))
            else:
                # 文本分享也能被 OTA 拉走：落一个临时文件给设备
                staged = self.settings.tmp_dir / f"send-{secrets.token_hex(8)}.txt"
                staged.write_text(row["text_content"] or "", encoding="utf-8")
            payload = FilePayload(
                path=staged,
                file_name=row["file_name"] or f"{row['id']}.txt",
                content_type=row["content_type"],
                size=row["size"],
            )
            view = self._view(row)
        return view, payload

    # ---------------------------------------------------------------- 清理

    def purge(self, now: int | None = None) -> dict[str, int]:
        """物理清理：过期 / 次数用尽 / 已焚毁的墓碑。

        * 活跃但已过期的分享 -> 先焚毁（删内容）
        * 已经 burned 且超过 ``tombstone_hours`` -> 连行一起删
        * 过期的 OTA token -> 删

        返回各类计数，方便 cron 日志里看一眼。
        """
        moment = now or _now()
        burned = 0
        removed = 0
        ota_removed = 0
        tombstone_cutoff = moment - self.settings.tombstone_hours * 3600
        with transaction(self.settings.db_path) as conn:
            expired = conn.execute(
                "SELECT * FROM shares WHERE burned_at IS NULL AND expires_at IS NOT NULL"
                " AND expires_at <= ?",
                (moment,),
            ).fetchall()
            for row in expired:
                if self.burn_if_expired(conn, row):
                    burned += 1

            exhausted = conn.execute(
                "SELECT * FROM shares WHERE burned_at IS NULL AND max_reads >= 0"
                " AND read_count >= max_reads",
            ).fetchall()
            for row in exhausted:
                self._burn(conn, row, "exhausted")
                burned += 1

            cur = conn.execute(
                "DELETE FROM shares WHERE burned_at IS NOT NULL AND burned_at <= ?",
                (tombstone_cutoff,),
            )
            removed = cur.rowcount or 0

            cur = conn.execute(
                "DELETE FROM ota_tokens WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (moment,),
            )
            ota_removed = cur.rowcount or 0

            # 审计日志不无限增长
            conn.execute("DELETE FROM events WHERE at <= ?", (tombstone_cutoff,))
        self.prune_orphans()
        return {"burned": burned, "removed": removed, "ota_removed": ota_removed}

    def prune_orphans(self) -> int:
        """删掉数据库里没有对应行的 blob 文件（进程崩溃留下的半成品）。"""
        with session(self.settings.db_path) as conn:
            known = {
                row["blob"]
                for row in conn.execute("SELECT blob FROM shares WHERE blob IS NOT NULL")
            }
        if not self.settings.blob_dir.exists():
            return 0
        count = 0
        for path in self.settings.blob_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.settings.blob_dir).as_posix()
            if relative not in known:
                path.unlink(missing_ok=True)
                count += 1
        for path in self.settings.tmp_dir.glob("*"):
            if path.is_file() and (time.time() - path.stat().st_mtime) > 3600:
                path.unlink(missing_ok=True)
        return count


def hmac_eq(a: str | None, b: str | None) -> bool:
    import hmac

    return hmac.compare_digest((a or "").encode(), (b or "").encode())


_STATE_ERRORS = {
    "burned": ("burned", "内容已焚毁"),
    "expired": ("expired", "链接已过期"),
    "exhausted": ("exhausted", "内容已焚毁（次数用尽）"),
}


def _state_error(state: str) -> str:
    return _STATE_ERRORS.get(state, ("gone", ""))[0]


def _state_message(state: str) -> str:
    return _STATE_ERRORS.get(state, ("gone", "内容不可用"))[1]


__all__ = ["Service", "ShareError", "Created", "FilePayload", "hmac_eq"]
