"""命令行入口。

    python -m bfs serve            # 起服务（默认 127.0.0.1:8000）
    python -m bfs purge            # 手动清理一次过期/用尽的内容
    python -m bfs info             # 打印配置与统计
"""

from __future__ import annotations

import argparse
import json
import sys

from .app import create_app
from .config import Settings
from .db import init_db, session
from .service import Service


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = Settings.from_env()
    if args.host:
        settings = _replace(settings, host=args.host)
    if args.port:
        settings = _replace(settings, port=args.port)
    if args.data_dir:
        settings = _replace(settings, data_dir=args.data_dir)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level)
    return 0


def cmd_purge(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    init_db(settings.db_path)
    service = Service(settings)
    print(json.dumps(service.purge(), ensure_ascii=False))
    return 0


def cmd_info(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    init_db(settings.db_path)
    with session(settings.db_path) as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN burned_at IS NULL THEN 1 ELSE 0 END) AS alive,
                 SUM(CASE WHEN burned_at IS NOT NULL THEN 1 ELSE 0 END) AS burned,
                 COALESCE(SUM(size), 0) AS bytes
               FROM shares"""
        ).fetchone()
    info = {
        "data_dir": str(settings.data_dir),
        "db": str(settings.db_path),
        "shares_total": row["total"],
        "shares_alive": row["alive"] or 0,
        "shares_burned": row["burned"] or 0,
        "bytes": row["bytes"],
        "limits": {
            "max_file_mb": settings.max_file_bytes // (1024 * 1024),
            "max_text_kb": settings.max_text_bytes // 1024,
            "max_ttl_hours": settings.max_ttl_hours,
            "max_reads": settings.max_reads_limit,
        },
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def _replace(settings: Settings, **kwargs) -> Settings:
    from dataclasses import replace

    return replace(settings, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bfs", description="阅后即焚快传服务")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--data-dir")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    purge = sub.add_parser("purge", help="清理过期与已焚毁的记录")
    purge.set_defaults(func=cmd_purge)

    info = sub.add_parser("info", help="查看配置与统计")
    info.set_defaults(func=cmd_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
