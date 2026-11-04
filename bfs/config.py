"""运行配置。

全部来自环境变量（前缀 ``BFS_``），不读配置文件：
这个服务要能在 docker-compose 里一条命令起起来，配置项越少越好。

``Settings`` 是 frozen dataclass —— 进程内只有一份，测试时直接构造新的。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PREFIX = "BFS_"

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _env(name: str) -> str | None:
    value = os.environ.get(PREFIX + name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # 配置写错要立刻炸，别静默回退
        raise ValueError(f"环境变量 {PREFIX}{name} 需要整数，当前为 {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    #: 单个文件上限（字节）
    max_file_bytes: int = 100 * 1024 * 1024
    #: 单份文本上限（字节，UTF-8）。粘贴一整段日志会很自然地超过 512 KB
    max_text_bytes: int = 2 * 1024 * 1024
    #: 不传 ttl 时的默认有效期（小时）；0 = 永不过期
    default_ttl_hours: int = 24
    #: 有效期上限（小时），防止有人建「永久」链接
    max_ttl_hours: int = 24 * 30
    #: 不传 max_reads 时的默认可读次数；-1 = 不限
    default_max_reads: int = 1
    #: 可读次数上限
    max_reads_limit: int = 100
    #: 开门后拿到的 access ticket 有效期（分钟）
    access_ttl_minutes: int = 30
    #: 每个 IP 每分钟允许的写操作（建分享 / 开门 / 建 OTA）
    rate_limit_per_minute: int = 30
    #: OTA 直链默认有效期（小时）与次数
    ota_ttl_hours: int = 24
    ota_default_uses: int = 1
    #: 对外域名（反代场景）。留空则用请求的 base_url。
    base_url: str = ""
    #: 挂载前缀（`BFS_PATH_PREFIX`）。留空 = 挂在域名根上；填 `bfs` = 整个服务
    #: 住在 `/bfs/` 下，页面 / API / 静态资源 / 分享链接都在这个前缀里面。
    path_prefix: str = ""
    #: 分享链接的路径段。留空 = 自动：有前缀时用前缀本身（`/bfs/<id>`），
    #: 没前缀时用 `s`（`/s/<id>`）。
    share_segment: str = ""
    #: 焚毁 / 过期的记录保留多久（小时），到点连「墓碑」一起删
    tombstone_hours: int = 24 * 7
    #: 内置清理任务的间隔（秒）
    purge_interval_seconds: int = 600
    #: 不传提取码时是否自动生成一个（默认开）。关掉 = 默认不设提取码。
    auto_extract_code: bool = True
    #: 是否记录审计事件
    audit: bool = True

    # ---- 管理员闸门 ----
    #: 管理员密码。设了才开启登录（写入口需要登录，读入口始终公开）。
    admin_password: str = ""
    #: 可选的用户名，设了就要求「用户名 + 密码」两项
    admin_user: str = ""
    #: 登录态有效期（小时）
    session_ttl_hours: int = 24 * 7
    #: GitHub OAuth App
    github_client_id: str = ""
    github_client_secret: str = ""
    #: 允许登录的 GitHub 用户名（逗号分隔）。空 = 即便配了 OAuth 也不放人进。
    github_allowed_users: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bfs.sqlite3"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "secret.key"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.blob_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_env("DATA_DIR") or "./data").expanduser()
        return cls(
            data_dir=data_dir,
            host=_env("HOST") or "127.0.0.1",
            port=_int("PORT", 8000),
            max_file_bytes=_int("MAX_FILE_MB", 100) * 1024 * 1024,
            max_text_bytes=_int("MAX_TEXT_KB", 2048) * 1024,
            default_ttl_hours=_int("DEFAULT_TTL_HOURS", 24),
            max_ttl_hours=_int("MAX_TTL_HOURS", 24 * 30),
            default_max_reads=_int("DEFAULT_MAX_READS", 1),
            max_reads_limit=_int("MAX_READS", 100),
            access_ttl_minutes=_int("ACCESS_TTL_MINUTES", 30),
            rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 30),
            ota_ttl_hours=_int("OTA_TTL_HOURS", 24),
            ota_default_uses=_int("OTA_DEFAULT_USES", 1),
            base_url=(_env("BASE_URL") or "").rstrip("/"),
            path_prefix=_env("PATH_PREFIX") or "",
            share_segment=_env("SHARE_SEGMENT") or "",
            tombstone_hours=_int("TOMBSTONE_HOURS", 24 * 7),
            purge_interval_seconds=_int("PURGE_INTERVAL_SECONDS", 600),
            auto_extract_code=(_env("AUTO_EXTRACT_CODE") or "1").lower()
            not in {"0", "false", "off"},
            audit=(_env("AUDIT") or "1").lower() not in {"0", "false", "off"},
            admin_password=_env("ADMIN_PASSWORD") or "",
            admin_user=_env("ADMIN_USER") or "",
            session_ttl_hours=_int("SESSION_TTL_HOURS", 24 * 7),
            github_client_id=_env("GITHUB_CLIENT_ID") or "",
            github_client_secret=_env("GITHUB_CLIENT_SECRET") or "",
            github_allowed_users=_env("GITHUB_ALLOWED_USERS") or "",
        )

    def public_url(self, base: str, share_id: str) -> str:
        """分享链接的绝对地址。

        ``base`` 是请求推算出的 origin（可能已含前缀），``base_url`` 配置优先 ——
        因为服务通常只在内网可达，真正给外部用的是 nginx 那个域名。

        去重规则：``base_url`` 里已经写了挂载前缀（`https://x.com/bfs`）就不再拼一次。
        否则两边都带前缀会得到 `/bfs/bfs/<id>` —— 这类重复拼接很难一眼看出来。
        """
        return self.external_url(base, self.share_path(share_id))

    def external_url(self, base: str, path: str) -> str:
        """``origin + 完整路径``，用于 OAuth 回跳这类必须是绝对地址的场景。"""
        return f"{self._root(base)}{path}"

    def _root(self, base: str) -> str:
        root = (self.base_url or base).rstrip("/")
        prefix = self.prefix
        if prefix and root.endswith(prefix):
            root = root[: -len(prefix)]
        return root

    def share_path(self, share_id: str) -> str:
        """分享页的完整路径（已含挂载前缀）：``/bfs/<id>`` 或 ``/s/<id>``。"""
        return f"{self.share_prefix}/{share_id}"

    @property
    def prefix(self) -> str:
        """规范化的挂载前缀（`""` 或 `/bfs`）。"""
        raw = (self.path_prefix or "").strip().strip("/")
        return f"/{raw}" if raw else ""

    @property
    def share_prefix(self) -> str:
        """**完整**的分享路径前缀（已含挂载前缀）。

        三种情况：
          * 显式 ``share_segment``  -> ``/bfs/s``
          * 有挂载前缀               -> ``/bfs``（分享页就住在服务根上）
          * 都没有                   -> ``/s``
        """
        raw = (self.share_segment or "").strip().strip("/")
        if raw:
            return f"{self.prefix}/{raw}"
        return self.prefix or "/s"

    @property
    def api_prefix(self) -> str:
        """API 前缀：带挂载前缀时用 `/bfs/api`，否则 `/api`。"""
        return f"{self.prefix}/api"

    # ---------------------------------------------------------------- 闸门

    @property
    def github_ready(self) -> bool:
        """OAuth 三要素齐了才算可用 —— 少一个就当作没配，别让人卡在 GitHub 错误页。"""
        return bool(
            self.github_client_id
            and self.github_client_secret
            and self.github_allowed_users.strip()
        )

    @property
    def gate_enabled(self) -> bool:
        """有任意一种登录方式就开闸门；都没配就保持公开（本地开发不会被挡住）。"""
        return bool(self.admin_password) or self.github_ready

    @property
    def github_allowed_set(self) -> set[str]:
        return {
            part.strip().lower()
            for part in self.github_allowed_users.replace(";", ",").split(",")
            if part.strip()
        }

    @property
    def login_path(self) -> str:
        """登录页（在页面前缀下）：`/bfs/login` 或 `/login`。"""
        return f"{self.prefix}/login"

    @property
    def oauth_callback_path(self) -> str:
        """OAuth 回跳：必须和 GitHub OAuth App 里填的 Callback URL 的路径一致。"""
        return f"{self.api_prefix}/auth/github/callback"
