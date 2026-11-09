"""SQLite 存储层。

为什么是 SQLite 而不是 Redis：
  * 单机部署、无外部依赖，整个服务就是一个 Python 进程 + 一个数据目录；
  * 「阅后即焚」要的是**原子递减**，不是内存里的计数器 —— SQLite 的
    ``BEGIN IMMEDIATE`` 就够用，进程重启也不会把已焚毁的分享复活。

时间一律存 Unix 秒（整数）：比较、TTL 计算都不依赖时区/字符串格式。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shares (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('text', 'file')),
    file_name         TEXT,
    content_type      TEXT NOT NULL,
    size              INTEGER NOT NULL,
    sha256            TEXT NOT NULL,
    md5               TEXT NOT NULL,
    crc32             TEXT NOT NULL,
    blob              TEXT,               -- 相对 data_dir；文本分享为 NULL
    text_content      TEXT,               -- 文本分享的正文
    markdown          INTEGER NOT NULL DEFAULT 0,  -- 文本分享是否按 Markdown 渲染
    detected_kind     TEXT,               -- 识别出的形态：code/log/diff/table/json/...
    language          TEXT,               -- 代码语言（code 形态才有）
    extract_hash      TEXT,               -- 提取码的 pbkdf2，NULL = 无提取码
    extract_salt      TEXT,
    manage_token      TEXT NOT NULL,      -- 上传者本地凭证：提前焚毁 / 建 OTA 直链
    created_at        INTEGER NOT NULL,
    expires_at        INTEGER,            -- NULL = 不过期
    max_reads         INTEGER NOT NULL,   -- -1 = 不限次数
    read_count        INTEGER NOT NULL DEFAULT 0,
    last_read_at      INTEGER,
    burned_at         INTEGER,             -- 有值 = 已焚毁（内容已删）
    -- 「关闭才销毁」用的两阶段：打开时只占位，关闭时才真正扣次数 + 删盘
    claim_token       TEXT,                -- 谁正在看这份内容
    claim_expires_at  INTEGER,             -- 占位超时：超时就当作已消费（防止只开不关）
    owner_id          TEXT,               -- 登录用户 subject；CI 用 shared
    -- 这次分享里有几个文件（明细在 share_files）。0 = 纯文本分享。
    -- 冗余这一列是为了列表页不必 JOIN 就能显示「3 个文件」。
    file_count        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_shares_expires ON shares (expires_at);

-- 一次发布里的多个文件。
--
-- 为什么单开一张表而不是「一个分享一个文件」：
-- 用户按 Ctrl+V 粘一段代码 + 拖两个文件，这在语义上是**一次分享**，
-- 应该是同一条链接、同一个提取码、同一次焚毁。旧实现是每个文件建一条
-- 分享，于是发一次东西会得到 N 条消息 —— 那不对。
--
-- shares 行仍是唯一的「分享」，`file_count` 冗余记录数量方便列表展示；
-- 单个文件的元信息（名称/大小/摘要）落在本表。
CREATE TABLE IF NOT EXISTS share_files (
    share_id     TEXT NOT NULL REFERENCES shares (id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,      -- 展示顺序，从 0 开始
    file_name    TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    md5          TEXT NOT NULL,
    crc32        TEXT NOT NULL,
    blob         TEXT NOT NULL,         -- 相对 data_dir
    detected_kind TEXT,                 -- 文本类文件的识别结果（可用于预览）
    language     TEXT,
    PRIMARY KEY (share_id, idx)
);

CREATE TABLE IF NOT EXISTS ota_tokens (
    token        TEXT PRIMARY KEY,
    share_id     TEXT NOT NULL REFERENCES shares (id) ON DELETE CASCADE,
    label        TEXT,
    max_uses     INTEGER NOT NULL,
    uses         INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    last_used_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_ota_share ON ota_tokens (share_id);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       INTEGER NOT NULL,
    share_id TEXT,
    kind     TEXT NOT NULL,
    ip       TEXT,
    detail   TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);

-- CI 上传凭据。只有 OAuth 模式才会写入这张表（见 bfs/tokens.py）。
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash   TEXT PRIMARY KEY,      -- sha256(明文)；明文不落库
    owner_id     TEXT NOT NULL,         -- 归属的 OAuth 用户 subject
    label        TEXT,                  -- 用户自己起的名字，如 "ci-firmware"
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_owner ON api_tokens (owner_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """写事务：``BEGIN IMMEDIATE`` 先拿写锁，避免「两个请求同时读到还有次数」。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


#: 老库升级用的补丁（``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列）。
#: 每一项都是 (列名, 列定义)，只加不改 —— 不动已有数据。
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "shares": [
        ("markdown", "INTEGER NOT NULL DEFAULT 0"),
        ("detected_kind", "TEXT"),
        ("language", "TEXT"),
        ("claim_token", "TEXT"),
        ("claim_expires_at", "INTEGER"),
        ("owner_id", "TEXT"),
        ("file_count", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
