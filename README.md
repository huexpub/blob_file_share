# BFS · 粘什么传什么

把代码、日志、表格、diff、堆栈、图片、文件直接 `Ctrl+V` 粘进来，
自动识别是什么、按最合适的样子排版，生成一条阅后即焚的链接。

## 快速开始

```bat
run.bat
```

或手动：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m bfs serve      # → http://127.0.0.1:8000
```

## 功能

**发送端**

- 粘贴后自动识别形态：代码（30+ 语言高亮）、日志（级别着色）、表格、diff、
  JSON、Markdown、报错堆栈、链接列表、纯文本、图片
- 识别错了可以下拉手动指定
- 生成后自动复制一段可直接转发的话：`我分享给你一段代码：<链接> 分享码 1234`
- 提取码默认自动生成，也可以不要
- 发送历史收在顶栏「历史」抽屉里，能看到每条「待读取 / 已读 N 次 / 已焚毁」

**接收端**

- 有提取码时全屏弹窗（背景虚化，4 格逐位输入）
- 验证码通过后直接显示内容，没有多余信息
- 文本类文件（`.md` `.txt` `.log` `.py` `.csv`）直接渲染；PDF / 图片内联预览；
  固件给 hexdump；其它类型给下载
- 复制 / 下载按钮固定在内容右侧，只显示图标，悬停出文字
- 内容超一屏时出现「回到顶部」小火箭

**销毁**

- 打开时先取内容但不扣次数，**关闭浏览器页面才算消费**
- 次数用尽、过期、手动焚毁，都会立刻删除磁盘上的文件
- 关页面时用 `sendBeacon` 提交；浏览器崩溃则由 180 秒占位超时兜底

## Docker

镜像由 GitHub Actions 构建，公开可拉：

```bash
docker run -d --name bfs -p 8000:8000 \
  -v bfs-data:/app/data \
  ghcr.io/huexpub/blob_file_share:latest
```

或直接用仓库里的 `docker-compose.yml`（`image` 已指向该镜像）：

```bash
docker compose up -d
```

## 配置

| 变量 | 说明 |
| --- | --- |
| `BFS_DATA_DIR` | 数据目录，默认 `./data` |
| `BFS_BASE_URL` | 复制出去的链接用哪个域名 |
| `BFS_PATH_PREFIX` | 挂在子路径下，如 `bfs` → `/bfs/...` |
| `BFS_MAX_FILE_MB` | 单文件上限，默认 100 |
| `BFS_MAX_TEXT_KB` | 单份文本上限，默认 2048 |
| `BFS_AUTO_EXTRACT_CODE` | 不填提取码时是否自动生成，默认开 |
| `BFS_DEFAULT_MAX_READS` | 默认可读次数，默认 1（阅后即焚） |

完整列表见 `bfs/config.py`。

### 反代到子路径

```yaml
# docker-compose.yml
environment:
  BFS_PATH_PREFIX: bfs
  BFS_BASE_URL: https://view.lotenall.com/bfs
```

```nginx
location /bfs/ {
    proxy_pass http://127.0.0.1:8000/;
    client_max_body_size 120m;
}
```

## 常用命令

```bash
python -m bfs serve          # 启动
python -m bfs purge          # 清理过期与已焚毁的记录
python -m bfs info           # 数据目录与统计
python -m pytest -q          # 测试
```

## 技术栈

Python 3.10+ / FastAPI / SQLite（WAL）。
前端是手写静态文件，无构建步骤，样式复用工作台的 `wk-*` 设计令牌。
内容识别与渲染都在服务端（`bfs/analyze.py`），所以发送端预览和接收端正文必然一致。
