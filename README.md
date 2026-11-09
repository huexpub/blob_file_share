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

## 登录（部署到公网时必看）

把服务放到公网后，**建分享的口子不能开着**，否则任何人都能往你的机器上写文件。
配置任意一种登录方式即自动开启闸门：

```bash
# 方式一：一个密码。够用、零依赖，推荐。
BFS_ADMIN_PASSWORD=换成你自己的

# 方式二：GitHub 登录（三项都要填，少一项不生效）
BFS_GITHUB_CLIENT_ID=xxx
BFS_GITHUB_CLIENT_SECRET=xxx
BFS_GITHUB_ALLOWED_USERS=huexpub      # 只有这个账号能登录

# 方式三：Gitea 登录（自建实例，三项都要填）
BFS_GITEA_URL=https://git.example.com
BFS_GITEA_CLIENT_ID=xxx
BFS_GITEA_CLIENT_SECRET=xxx

# 可以混配，登录页会同时给出所有可用入口
```

配置 GitHub 时，OAuth App 的 **Authorization callback URL** 要填：

```
https://<你的域名>/bfs/api/auth/github/callback     # 挂了前缀 BFS_PATH_PREFIX=bfs
https://<你的域名>/api/auth/github/callback         # 根部署
```

Gitea 类似，把 `github` 换成 `gitea`：

```
https://<你的域名>/bfs/api/auth/gitea/callback
```

这些值统一写在 `.env` 里（`cp .env.example .env` 后填），不要改 `docker-compose.yml` ——
`.env` 已被 `.gitignore` 忽略，改 compose 会把密钥提交上去。

要让 OAuth 走对外域名而不是内网地址，同时设好 `BFS_BASE_URL`。

**闸门只锁写入口** —— 建分享、查历史、焚毁、OTA 直链需要登录；
接收方打开链接、输提取码、看内容、下载 **始终不需要账号**。
`/api/inspect`（粘贴预览）也保持公开，分享页渲染 `.md`/`.log` 正文要靠它。

不配任何登录方式 = 保持原来的全公开模式，适合本机或纯内网。

| 变量 | 说明 |
| --- | --- |
| `BFS_ADMIN_PASSWORD` | 管理员密码，设了就开闸门 |
| `BFS_ADMIN_USER` | 可选用户名，设了要「用户名 + 密码」两项 |
| `BFS_SESSION_TTL_HOURS` | 登录态有效期，默认 168（7 天） |
| `BFS_GITHUB_CLIENT_ID` / `_SECRET` | OAuth App 的凭据 |
| `BFS_GITHUB_ALLOWED_USERS` | 允许登录的 GitHub 用户名，逗号分隔 |
| `BFS_GITEA_URL` | Gitea 实例地址，如 `https://git.example.com` |
| `BFS_GITEA_CLIENT_ID` / `_SECRET` | Gitea OAuth 应用凭据 |

## 管理模式与 CI 上传

**只有启用了 OAuth（GitHub 或 Gitea）才是「管理模式」**，才会出现账户概念。
纯密码模式仍然是「单人管理」：能分享，但没有用户历史，也没有 CI token。

开启 OAuth 后：

- 分享历史存数据库、按账户隔离，换设备也看得到
- 在历史抽屉里点「CI Token」自助创建，明文只显示一次
- 删除历史 = 立即焚毁内容（不是只从列表里移除）

### 给 CI 用的上传接口

```bash
curl -X POST https://<域名>/bfs/api/shares/batch \
  -H "Authorization: Bearer bfs_xxxxxxxxxxxx" \
  -F "file=@firmware.bin" \
  -F "file=@symbols.zip"
```

响应里的 `message` 可以直接转发出去：

```
我分享给你 2 个文件：
1. firmware.bin
   https://<域名>/bfs/s/abcd1234
   分享码 1379
2. symbols.zip
   https://<域名>/bfs/s/efgh5678
   分享码 2468
```

默认**永久有效、不限次数**（构建产物是给人长期下载的），
可用 `BFS_API_DEFAULT_TTL_HOURS` 改；请求里也能带 `ttl_hours` / `max_reads` 覆盖。

用 token 上传的内容归属 token 的所有者，所以会出现在本人的历史里，
和网页端分享一起管理、一起焚毁。撤销 token 后流水线立即失败。

### 文件存在哪

分享的文件**直接落在磁盘上**（不是内存），路径是 `BFS_DATA_DIR/blobs/<前两位>/<id>`：

```bash
docker compose exec bfs ls -R /app/data/blobs | head
```

compose 里把宿主目录挂到 `/app/data`，所以你的文件实际在 `/opt/bfs/blobs/`。
分享被焚毁、次数用尽、过期或手动删除时，对应文件立即 `unlink`，
数据库只留一条墓碑记录（时间、大小、sha256，正文已清空）。

文本分享的正文存在 SQLite（`text_content`），因为要参与识别与渲染；
它同样在焚毁时被置为 NULL。

### 页脚（备案号）

`BFS_FOOTER_HTML` 支持 HTML，用来放备案号这类带链接的信息：

```bash
BFS_FOOTER_HTML=<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">粤ICP备00000000号-1</a> · BFS
```

留空则显示内置的 `BLOB FILE SHARING @ 2026`。
内容由服务端注入所有页面（首页 / 分享页 / 登录 / 历史 / 设置），不写死在 HTML 里。

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
