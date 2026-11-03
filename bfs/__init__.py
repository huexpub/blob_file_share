"""BFS —— 阅后即焚的文本 / 文件快传服务。

模块划分：

    config.py   环境变量 -> Settings（不可变）
    db.py       SQLite schema + 连接（WAL）
    service.py  唯一的业务写入口：建分享、开门、消耗、OTA、清理
    api.py      /api/* JSON 接口
    web.py      页面路由（/、/s/<id>）与静态资源
    app.py      create_app()：装配 FastAPI
    cli.py      serve / purge / info
"""

__version__ = "0.1.0"
