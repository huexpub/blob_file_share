# BOM 工具（Altium PcbDoc / SchDoc → 生产 / 采购 BOM 与差异产物）

从 `.PcbDoc` 直接读 **位号 / 值 / 封装 / 耐压**，自动归一化并合并 BOM 行，
映射一次后记住（别名 + 规则），以后同一块板或同系列板自动命中，导出 Excel。

`.SchDoc`（原理图）同样可解析：位号 / 值 / 封装进同一套 BOM 流程，并输出
`元件 + 引脚 + 网络` 的结构化 JSON 与分层 SVG，供平台做版本差异对比。

- 后端：Python 3.10+ / FastAPI / SQLAlchemy（SQLite 本地，Postgres 可换）
- 解析：`olefile` 纯 Python 读 PcbDoc / SchDoc，无 COM、不依赖 Altium，可在 Linux/Docker 运行
- 前端：单文件 HTML（无需构建），映射表 + 料号库 + 规则 + 原理图 + 历史

## 快速开始（Windows，本地）

```bat
run.bat
```

首次运行会自动建 `.venv` 并装依赖，然后打开 <http://127.0.0.1:8000>。

手动方式（必须使用同一个虚拟环境解释器）：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe main.py
```

如果直接执行系统 `python main.py` 报 `ModuleNotFoundError`，说明当前解释器
没有安装项目依赖；不要混用系统 Python 与 `.venv`。Windows 可直接运行
`run.bat`，它会创建 `.venv` 并安装运行依赖。

## 命令行

```bash
python -m apps.cli parse  board.PcbDoc -o bom.xlsx     # 解析并导出 Excel
python -m apps.cli verify board.PcbDoc board_pnp.csv   # 用坐标文件校验解析结果
python -m apps.cli parts                               # 查看料号库
python -m apps.cli parts --add RES-10K-0402 10K 0402   # 新增料号
python -m apps.cli seed parts.csv                      # 批量导入料号 (code,value,footprint,...)
python -m apps.cli serve --port 8000                   # 启动 Web 界面
python -m apps.cli convert board.PcbDoc -o dist        # 确定性产物包（见下）
python -m apps.cli convert design.SchDoc -o dist       # 原理图产物包
python -m apps.cli offline board.PcbDoc -o offline     # 自包含离线包（双击即开）
python -m apps.cli purge --days 30                     # 清理过期分享与无人引用的快照
python -m apps.cli schdoc design.SchDoc -o dist        # 只要 schematic.json / .svg
python -m apps.cli inspect-schdoc design.SchDoc        # 记录类型统计（排查用）
```

### 工作台（`/admin`）

前端是 **Vue 3 + Vite 手写**（不依赖组件库，也不用 CSS 框架，产物 ~180KB）。
结构参考 [KiCAD-Prism](https://github.com/krishna-swaroop/KiCAD-Prism) 的
「项目空间」信息架构：以项目为主体，而不是一排后台表格。

```
工作台 /                    顶栏：logo · 搜索（居中） · 配色
                            左栏：Workspace（项目 / 分享）
                            工具条：导入项目 · 刷新 · 设置
 ├─ 项目画廊                卡片：320px 正方形缩略图（预留板图/3D）+ 名称/说明/版本数
 └─ 项目详情 /project/:id   顶栏：‹ 返回 · 项目名（锁定时带版本号） · 同步/配色/设置
                            左栏：概览 · 历史 · 设计 · 分享
      ├─ 概览   说明 + 最新版本统计 + 最近分享
      ├─ 历史   版本列表，勾选两版 → 对比；点「查看」→ 锁定该版本进设计
      ├─ 设计   原查看器直接嵌入（Shadow DOM，非 iframe）：BOM 映射 / PCB 图层 /
      │         板图 / 原理图 / 3D 全在里面
      └─ 分享   发布、复制、暂停、延期、关闭
```

默认看最新版本；只有在历史里点「查看」才锁定某个版本，顶栏会标出它是哪一版。

**一个版本 = 一组文件，不是一个文件**。后端为了解析和产物按文件建行
（一个 `.PcbDoc` 一行、每个 `.SchDoc` 一行），但产品语义上同一次 Git 提交
（或同一个人工版本号）下的全部文件属于同一个项目版本：通常一个 PCB + 若干原理图。
工作台在展示层用 `commit_sha`（Git）或 `version`（手工）折叠成「版本组」：
历史一行代表一版并在下方列出组内文件；设计区顶部是文件 tab，默认打开 PCB，
其余原理图作为同级 tab（多个 SchDoc 用文件名区分）。版本对比、发布分享、删除都按
整组进行，删掉一个版本会连同它的原理图一起删。

**一版必须「恰好 1 个 PcbDoc + 至少 1 个 SchDoc」**（`bomkit/versions.py`）。

- 不满足时（缺原理图、缺 PCB、出现第二个 PcbDoc —— 通常是备份副本），这一版
  **不解析、不生成任何产物、不画封面、不可分享**，只把中文原因写进
  `designs.problems`；补齐文件后自动恢复，删掉多余文件同理。
- 判定在**版本**层而不是文件层：先登记源文件、再体检、再决定要不要解析。
  这样半个版本不会**先**发布出去，也不会在画廊里抢走封面。
- Git：**一个 commit = 一个版本**。同步时先比对「文件名 + 内容」集合，集合相同
  （只改文档的提交）不产生新版本；集合不同（哪怕只改了一张原理图）就整版重新
  入库 —— 没变的 PCB 也会在新版里有一行，否则新版只剩半个文件。
- 上传：走「版本优先」的 `POST /api/projects/{id}/versions`（一次多选文件），
  补文件用 `POST /api/projects/{id}/versions/{version}/files`；
  `GET /api/projects/{id}/versions` 返回按版本聚合的列表（含体检结果）。
  旧的单文件接口仍然可用，但同样要等整版凑齐才生成。
- 画廊的「N 个版本」取 `version_count`（不是文件行数 `design_count`）；封面只从
  **完整版本**里取，`cover.webp`（浏览器渲染的 3D 截图）优先，其次 `cover.svg`
  （几何俯视图），并且会检查文件确实在盘上。
- 历史数据可用 `POST /api/maintenance/audit`（`{"apply": true}`）一次性体检：
  写回 `problems`、收回不完整版本的产物；`{"apply": true, "rebuild": true}`
  会顺带补齐完整版本缺失的产物。`/api/maintenance/rebuild` 现在按**版本**重建。

- **导入项目**只有两条路径：**从 Git 同步**（标识/名称/说明/仓库地址/子目录，
  版本 = **完整 commit sha**）与**手动上传**（版本号可留空，留空则用文件
  `sha256` 的前 40 位，与 Git 的 40 位 commit 等长、同风格）。
- **分享管理**（`/shares`）跨项目列出已发出的分享，每行 = 项目/版本 + 链接 + 提取码 +
  状态 + 到期 + 浏览 + 大小/3D，操作有复制链接 / 复制提取码 / 暂停·恢复 / 延期 / 关闭 / 删除。
  `关闭` 不可恢复（只能删），`暂停` 可恢复，`purge` 不会删掉已暂停的分享。
- **版本对比**：`GET /api/projects/{id}/diff?a=&b=` 读两版已落盘的 `project.json`，
  逐位号比较值/封装/层/旋转/装配，另给 BOM 行的数量与位号增删；位置微调单独归类，
  不算电气变更。结果里带两侧版本号，前端不用二次查询。

> 前端只是平台的一部分：产品形态是「项目空间」，所以壳层自己写（不套后台模板）。
> 代价是导航/弹窗/表格要自己维护，好处是产品设定（深色、分区式项目页、无圆角）
> 不用和任何框架默认值搏斗。

**设计分区怎么复用老页面**：不复制代码、也不套 iframe。工作台把
`apps/local/static/index.html` 的样式与脚本搬进 **Shadow DOM**，并把脚本的
DOM 查询根指向影子树（`window.__BOM_VIEWER_HOST__`）。于是同一份查看器代码
（BOM 映射 / PCB 图层 / 板图 / 原理图 / 3D、联动、缩放、拖拽分栏）原样跑在
工作台里，而它自带的全局样式被影子边界挡住。查看器脚本暴露
`window.__bomViewer.{activate,clear,relayout,setTheme}` 给工作台调用。

**打开版本不重新解析**：版本 token 是 `d<design_id>`，`GET /api/boards/d<id>`
直接读导入时就落盘的 `project.json`；只有 3D 需要源文件里的 STEP，
走 `_ensure_board` 懒解析一次进 LRU。

**分享路由**：规范地址 `{LAYOUT_SHARE_PREFIX}/<slug>`，默认 `/layout/<slug>`（下载为
`/layout/<slug>/download`）。多个分享服务共用一个域名时，nginx 按首段分流：
`/layout/` → 本服务，`/doc/` → 文档服务。复制出的链接用 `LAYOUT_SHARE_SITE`
（如 `https://share.example.com`）拼绝对地址；留空则用当前页面 origin。
提取码 Cookie 每个 slug 一个（`layout_share_<slug>`），可同时解锁多个分享。

**Git 同步**：`POST /api/projects/{id}/sync`，只接受 `https://` 且主机在
`LAYOUT_GIT_HOSTS`（默认 github.com / gitlab.com / gitee.com / gitcode.com）内，
浅克隆单分支、禁 submodule、跳过 LFS；同一 commit 重复同步不会产生新快照。

**双端地址规则（`gitimport.clone_url_for`）**：项目里登记的 `git_url` 是给
**人**看的 —— 后台「打开仓库」是个 `<a href>`，必须是 https 才点得开；而服务端
克隆用的是部署机的 **SSH 密钥**（`~/.ssh` 只读挂载），没有交互终端可以输账号
密码。所以两者天然是两套地址，转换只在后端做一次：

```
https://git.example.com/layout/board.git
        ↓  clone_url_for()
git@git.example.com:layout/board.git
```

规则只有一条，没有开关：https → `git@主机:组织/仓库.git`；已经是 SSH 的原样
返回；其它形态原样返回，交给 `validate_url` 报错。**前端不做任何转换**，永远
只存、只显示 https。

自检：`POST /api/webhook/gitea` 的返回里有 `git_url`（登记的）和 `clone_url`
（实际用的）两个字段；失败时会给出中文原因（如 SSH 私钥 / known_hosts 问题）。

**Gitea Webhook（仓库改了自动同步）**：`POST /api/webhook/gitea`

```bash
# 服务端 + Gitea 的 Webhook 里填同一个值
LAYOUT_WEBHOOK_SECRET=<随机长串>
# 可选：只让这些分支触发（留空 = 不限）
LAYOUT_WEBHOOK_BRANCHES=main,master
```

Gitea → 仓库 → 设置 → Web 钩子 → 添加：目标 `https://<域名>/api/webhook/gitea`、
类型 `Gitea`、密钥填上面的值、触发条件只勾「推送」。

### 添加示例（`layout.lotenall.com`）

组织级（设置 → Web 钩子 → 添加 Web 钩子 → **Gitea**）或仓库级都行，字段填：

| 字段 | 值 |
|---|---|
| 目标 URL | `https://layout.lotenall.com/api/webhook/gitea` |
| HTTP 方法 | `POST` |
| POST 内容类型 | `application/json` |
| 密钥 | 与服务端 `LAYOUT_WEBHOOK_SECRET` **完全相同**的字符串 |
| 触发条件 | 只勾「推送事件」（Push）；其余不勾 |
| 激活 | 勾上 |

> 组织级钩子会应用到该组织**所有**仓库，这正是我们要的：同一条钩子覆盖
> `layout/*`，程序按 `repository` 里的仓库标识只同步**已添加的项目**，
> 没入库的仓库直接忽略。`kicad/tcpLock` 不在 `layout` 组织下，需要单独在
> 那个仓库里再加一条同样的钩子。

保存后 Gitea 会立刻发一次 `ping`，界面上应显示**绿色成功**、响应体形如
`{"ok":true,"event":"ping","synced":[],"reason":"ping：Webhook 配置成功"}`。
之后点「测试推送」或真的 push，响应体里会列出命中的项目：

```json
{"ok": true, "event": "push", "branch": "main",
 "repository": "layout/v1t485",
 "synced": [{"project": "v1t485", "branch": "main", "state": "running"}]}
```

**自己验一次签名**（不用真的 push，可确认密钥是否一致）：

```bash
SECRET='<你的 LAYOUT_WEBHOOK_SECRET>'
BODY='{"ref":"refs/heads/main","after":"b8ba0ed8","repository":{"full_name":"layout/v1t485"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)
curl -sS -X POST https://layout.lotenall.com/api/webhook/gitea \
  -H 'Content-Type: application/json' \
  -H 'X-Gitea-Event: push' \
  -H "X-Gitea-Signature: $SIG" \
  --data-binary "$BODY"
```

返回 `synced` 有内容 = 链路通了；`401 签名校验失败` = 密钥不一致；
`503` = 服务端没读到 `LAYOUT_WEBHOOK_SECRET`（改完 `.env` 要 `docker compose up -d`）。

**反向代理注意**：签名是对**原始请求体字节**算的，所以 nginx 不能改写 body
（不要开 `sub_filter`，`proxy_request_buffering` 保持默认即可），也不要吞掉
`X-Gitea-Signature` / `X-Gitea-Event` 这两个头：

```nginx
location = /api/webhook/gitea {
    proxy_pass http://bom-tool:8000;      # 去掉末尾斜杠，路径原样透传
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    # 逐跳头不要在这里过滤；Gitea 的自定义头默认就会被转发
    client_max_body_size 2m;              # 与 MAX_PAYLOAD_BYTES 对齐
}
```

`/api/webhook/gitea` 是**公网可达**的（Gitea 从自己的服务器发起），如果你的
nginx 用 `allow/deny` 把 `/api/` 限定在内网，要为这个路径单独放行。
也可以直接按 Gitea 的来源 IP 收紧，但签名才是真正的闸门。

- 这个接口**必须匿名可访问**（Gitea 不带我们的 Cookie），所以它的闸门是
  `X-Gitea-Signature` 的 HMAC-SHA256（对原始请求体签名，也接受 GitHub 风格的
  `X-Hub-Signature-256: sha256=…`）。**没配密钥直接 503** —— 一个能触发
  `git clone` 的公开接口若允许未签名调用，就是放大攻击入口。
- 仓库标识从载荷的 `repository.clone_url / ssh_url / full_name / html_url`
  归一化成 `host/owner/repo` 后与 `projects.git_url` 比对（去协议、端口、`.git`、
  大小写）。主机能对上才算命中；载荷里完全没有主机时才退回 `owner/repo`，
  避免「另一台机器上同名仓库」误触发。
- 只处理 `push`：`ping` 回 200（Gitea 界面上显示配置成功）；tag、分支删除、
  不在 `LAYOUT_WEBHOOK_BRANCHES` 内的推送都忽略。
- 命中后**异步**跑与手动同步完全相同的任务（同一项目同时只跑一个克隆）。
  默认最多等 20 秒（`LAYOUT_WEBHOOK_WAIT`）把**最终结论**一并返回，所以
  Gitea 的「最近投递」里能看到 `state / commit / imported / warnings / error`，
  而不是永远一句 `running`：

  ```json
  {"ok": true, "synced": [{"project": "ch390h", "branch": "main", "state": "done",
    "commit": "c70a231e…", "imported": 2, "warnings": [], "error": ""}]}
  ```

  没有匹配到项目时返回 `reason` 和 `known`（现存项目的 `git_url` 列表），
  用来核对组织名 / 大小写 / `.git` 后缀。
- 同步结果**落库**（`projects.sync_state / sync_error / sync_at / sync_detail`），
  重启后仍能查到「上次同步为什么没东西」；失败原因同时打到容器日志
  （`[sync] 项目 N 失败：…`）。画廊会显示「同步失败」标记，鼠标悬停看原因，
  并且每 20 秒静默刷新一次，webhook 推完不用手动按刷新。

**3D 烘焙**：服务端没有 CAD 内核，所以发布时快照只有板图/原理图/BOM。
点「烘焙」会打开 `/?token=<board>&bake=<snapshot>`，页面完成三角化（命中
IndexedDB 缓存则秒级）后把网格回传，**同一个快照 id 原地升级**，因此已经发出去的
分享链接无需重发就能显示 3D。

构建（产物已提交，只跑 Python 也能用）：

```bash
cd web
npm install
npm run build        # → apps/local/static/admin/（先 rimraf 再写）
npm run dev          # 开发模式 :8003，代理 /api、/layout 到 :8000
```

> **Docker 构建不需要先手动跑 `npm run build`**：`Dockerfile` 是多阶段的，
> `node:22-slim` 阶段会现场编译 `web/` 并用结果覆盖镜像里的
> `apps/local/static/admin/`，所以只要源码进了仓库镜像就是新的。仓库里那份产物
> 只是给「只装 Python」的场景兜底，忘记重建不会影响线上，只会让本地
> `python main.py` 看到旧界面。

`bom-tool offline` 生成一个目录，里面有：

```
index.html           数据 + three.js + 离线 3D 渲染器全部内联，file:// 双击即开
bom.xlsx             发布时已生成好的工作簿（离线页的「导出 Excel」就是这个文件）
manifest.json        成员清单
```

页面与在线版是**同一套代码**，只是处在「静态模式」：不显示上传、料号库、规则、历史，
BOM 料号列变为只读。板图缩放/平移、原理图缩放/平移、BOM ↔ 板图 ↔ 3D 联动高亮都保留。

服务端发布分享时用的是同一个构造器（`apps/local/offline.py`），区别只是数据从数据库读：

```bash
# 建项目 -> 上传版本 -> 发布分享（返回 slug + 4 位提取码）
curl -X POST localhost:8000/api/projects -H 'Content-Type: application/json' \
     -d '{"key":"tcpLock","name":"TCP Lock"}'
curl -X POST 'localhost:8000/api/projects/1/designs?version=A.02' -F file=@tcpLock.PcbDoc
curl -X POST 'localhost:8000/api/projects/1/shares' -H 'Content-Type: application/json' \
     -d '{"hours":168,"design_ids":[1]}'
# 打开 http://127.0.0.1:8000/layout/<slug>  输入提取码即可查看，/layout/<slug>/download 下载 zip
# 对比两个版本
curl 'localhost:8000/api/projects/1/diff?a=1&b=2'
```

### 把线上数据拉回本地看

服务跑在服务器上时，想在本地打开同一份数据调试：

```bash
docker compose exec bom-tool python -m apps.cli backup -o /app/data/backup.zip
# 再拷回本地
docker compose cp bom-tool:/app/data/backup.zip ./backup.zip
```

> **别直接 `scp data/layout.sqlite3`**：SQLite 开着 WAL，服务在跑时复制会拿到撕裂的
> 备份（打开报 `database disk image is malformed`）。`backup` 用 `VACUUM INTO`
> 做在线一致性导出，不用停服务。

本地查看：

```bash
unzip backup.zip -d _prod
LAYOUT_DATA_DIR=./_prod LAYOUT_AUTH_MODE=off python main.py   # → http://127.0.0.1:8000/admin/
```

默认跳过 `bundles/`（分享包体积大，且能由产物重新生成），需要连分享包一起带就用
`--with-bundles`。

设计身份是 `sha256(文件内容)`，同一份文件重复上传不会重复生成产物；
`version` 是必填语义字段（Git 用**完整 commit sha**，手动用业务版本号；留空则
取文件内容 sha256 的前 40 位）。它同时是磁盘目录名的一部分，所以同一版本的所有
文件天然落在同一个目录下。
分享指向的是**冻结的 zip**，所以解析器以后怎么改都不会影响已发出的链接。

数据目录（`LAYOUT_DATA_DIR`，默认 `./data`）：

```
layout.sqlite3                              parts / rules / projects / designs / snapshots / shares
projects/<项目标识>/<版本>/
    bom.xlsx                             这一版对外的 BOM（PCB 优先，见下）
    source/<原文件名>                    原始 PcbDoc/SchDoc，同版本的多个文件平铺共存
    artifacts/<原文件名>/                project.json / bom.xlsx / assembly.json / schematic.*
    models/                              这一版的 3D（见下）
        index.json                       清单：sha256 / 文件名 / 是否已烘焙
        <sha256>.step                    导入时从 PcbDoc 解压出的原件
        <sha256>.bin                     浏览器烘焙的三角网格（服务端做不了）
```

### 3D 数据为什么分两段（改之前先读）

| 阶段 | 谁来做 | 产物 |
| --- | --- | --- |
| 从 PcbDoc 的 `Models/<n>` 流解压出 STEP | **服务端**（纯 zlib） | `<sha256>.step` |
| STEP → 三角网格（可渲染） | **服务端**（Node + 仓库里的 OCCT WASM） | `<sha256>.bin` |

两段都在**导入时**完成，所以新导入的版本立刻就有完整 3D。

网格烘焙走 `apps/local/step_bake.js`：Node 里加载
`static/vendor/occt/occt-import-js.wasm`（7.4MB，浏览器用的同一份）和
`static/step-parse.js`（同一份颜色解析）。**复用同一套代码**，所以服务端烘焙的
网格与浏览器渲染逐字节一致 —— 不会出现「同一颗料在两处颜色/朝向不同」。

为什么不用 Python 的 OCCT 绑定：`cadquery-ocp` 的 wheel 约 87MB，新版还依赖
`vtk`（再 100MB+）；而且 OCP 是底层 OCCT，要自己遍历面取三角形、再用
`XCAFDoc_ColorTool` 取颜色，很难和浏览器那份对齐。用 Node 复用现成 WASM
几乎零额外体积，正确性还更高。

实测成本（20 个内嵌模型 → 去重成 10 个）：**导入多花约 1.7 秒**，
网格 1.15MB（base64）比 STEP 原件 1.43MB 还小。

控制开关：

| `LAYOUT_BAKE_MESHES` | 行为 |
| --- | --- |
| `auto`（默认） | 有 `node` 就烘焙，没有就静默跳过 |
| `on` | 强制烘焙，没有 Node 时报错（想确保线上一定有 3D） |
| `off` | 关闭（调试导入性能时用） |

烘焙失败**不影响导入**：版本照常入库，只是缺 `*.bin`，离线页 3D 显示
「待解析」，BOM/板图/原理图完全不受影响。用户之后打开 3D 仍会由浏览器回填
（`POST /api/designs/<id>/meshes`）。

> 本地开发机没装 Node 也没关系：镜像里装了 `nodejs`（见 `Dockerfile`），
> `auto` 模式下自动生效。

`models/` 放在**版本目录**下（不是项目下、也不是全局），因为「下载这一版」
必须自包含：换台机器、断网也能看到这一版的 3D。

### 下载一整版

`GET /api/designs/<id>/version-zip`（历史页的「下载」按钮）：

```
index.html            离线单页，双击即开（数据/BOM/板图/原理图内联）
source/<原文件名>     原始 PcbDoc/SchDoc
bom.xlsx              版本级 BOM
models/index.json     3D 清单
models/<sha>.step     3D 原件（导入时就有）
models/<sha>.bin      三角网格（导入时烘焙，与浏览器同源）
```

响应头 `X-Bom-Step-Count` / `X-Bom-Baked-Count` 直接说明 3D 的完整度。
正常情况两者相等；若烘焙被关闭（`LAYOUT_BAKE_MESHES=off`）或失败，
`baked` 会小于 `step`，此时离线页 3D 显示「待解析」，其余功能不受影响。

目录名用**业务字段**而不是数据库 id，`projects/tcpLock/<commit>/source/tcpLock.PcbDoc` 一眼能看出
是哪个项目、哪一版、哪个文件，不用回头查库。每个 `designs` 行的 `storage_dir`
列记着这个相对路径，读取时直接用；`storage_dir` 为空的老数据自动回退到
`artifacts/<id>`、`sources/<id>`，所以升级不需要迁移脚本。

> **为什么源文件能平铺、产物要再多一层**：同一版本的 `.PcbDoc` 与 `.SchDoc` 会产出
> **同名**产物（`project.json`、`bom.xlsx`），放同一目录会互相覆盖；而源文件名天然
> 不同（`tcpLock.PcbDoc` vs `tcpLock.SchDoc`），不需要额外嵌套。一个版本有多个
> SchDoc 时同样成立，增删任一文件都不会影响其它文件。

**一个版本只有一份对外 BOM**（版本目录下的 `bom.xlsx`）：

| 这一版的内容 | 版本 BOM 取自 |
| --- | --- |
| 有 `.PcbDoc` | **PCB 的 BOM**（生产口径） |
| 只有原理图（例如先传个 SchDoc 看看） | 原理图的 BOM |
| 一个可用的都没有 | 不生成 |

这不是「合并多份 BOM」，而是一条固定的取舍规则 —— 同一位号在 PCB 与 SchDoc 上
参数可能不同，合并必须先定「以谁为准」，而生产口径本来就以 PCB 为准。规则在入库
和删除时都会重算，所以「先传原理图、后补 PCB」时版本 BOM 会自动切换成 PCB 的；
删掉 PCB 又会降级回原理图的。下载走 `GET /api/designs/<id>/version-bom`，
历史页的「Excel」按钮就是它。

```
meshes/<sha256(STEP)>.bin                浏览器烘焙的网格，跨快照/跨项目去重
bundles/<snapshot_id>/bundle.zip         分享与下载的真身
```

> `/admin` 默认**没有鉴权**（`LAYOUT_AUTH_MODE=off`），`/api/projects`、`/api/shares`
> 也是开放的；它假定运行在受信任的内网或本机。对外暴露前请按下一节开启登录。

## 登录（Gitea OAuth）

`LAYOUT_AUTH_MODE=gitea` 打开后，`/admin` 和所有 `/api/*`（除白名单）都要登录；
**分享页 `/layout/<slug>` 不受影响**，依旧凭 4 位提取码公开访问。

公开工具页 `/`、`/3d`、`/gerber` **不需要登录**：任何人都能上传自己的 PCB/SchDoc 或 STEP，
前端只做解析与分组。匿名请求走的是「不查料号库」的分支（`POST /api/parse` 的白名单
入口），响应里不会带 `part_code` / 描述 / 库存 / 价格，`stats.matched` 恒为 0；
3D 装配与内嵌模型下载（`/api/3d/*`、`/api/models*`）同样匿名可用——它们读的是 token
对应那份上传文件自身的数据，不查料号库（外部传入、非 Git 导入的项目也靠公开页看 3D）。
`/gerber`（钢网拼版编辑器）是纯前端本地计算，文件不出浏览器，本来就不碰后端数据。
导出 Excel、原理图、规则/料号管理仍然要登录。公开页也不写任何项目历史
（不留 `localStorage` 记录），所以在 `/admin` 里看过的设计不会顺着首页漏出去。

在 Gitea 里建一个 OAuth2 应用（勾选**机密客户端**），重定向 URI 填
`https://<你的域名>/auth/callback`——**逐字符精确匹配，结尾不要加斜杠**。
例如部署在 `layout.lotenall.com` 时填：

```
https://layout.lotenall.com/auth/callback
```

> 拿不准就打开工作台「设置 → 登录」，那一行显示的 `重定向 URI` 就是该填的字符串
> （它由 `LAYOUT_AUTH_ORIGIN` 或请求本身的 origin 算出）。注意**先配好环境变量再去看**，
> 否则显示的会是内网地址。

然后配置这些环境变量：

| 变量 | 说明 |
| --- | --- |
| `LAYOUT_AUTH_MODE` | `off`（默认）/ `gitea` |
| `LAYOUT_GITEA_URL` | 实例根地址，如 `https://git.lotenall.com`（不要带 `/api`） |
| `LAYOUT_GITEA_CLIENT_ID` | OAuth2 应用 Client ID |
| `LAYOUT_GITEA_CLIENT_SECRET` | OAuth2 应用 Client Secret |
| `LAYOUT_AUTH_SECRET` | 签 session cookie 的密钥，随便一串长随机字符 |
| `LAYOUT_AUTH_ALLOW` | 允许登录的用户名，逗号分隔；留空 = 实例上任何账号 |
| `LAYOUT_AUTH_ORIGIN` | 反代后对外域名（如 `https://layout.lotenall.com`）；留空则从请求推断 |
| `LAYOUT_AUTH_DEV_BYPASS` | `1` 时跳过登录校验（**仅本机调试**，等同关闭鉴权） |

实现细节：状态码与 session 都用 `hmac` + `secrets` 自己签，**没有新增任何依赖**；
session cookie 是 `HttpOnly` + `SameSite=Lax` 的 7 天签名凭证，服务端不存 session 表。
`LAYOUT_AUTH_SECRET` 必须显式设置——留空会在进程内随机生成，重启即全员掉线，
多 worker（`LAYOUT_WORKERS>1`）下还会互相不认，所以**多进程部署时它是必需的**。

登录接口：`GET /auth/login?next=`（页面）、`GET /auth/start`、`GET /auth/callback`、
`POST /auth/logout`、`GET /api/auth/me`（返回 `{auth_enabled, user, login_url}`，
前端靠它决定是否显示登录入口；该接口本身公开，未登录时不含业务数据）。

部署在反代后面时**务必设 `LAYOUT_AUTH_ORIGIN`**：`redirect_uri` 与 Cookie 的 `Secure`
标志都以它为准，否则会算成 `http://127.0.0.1:8000/auth/callback` 这类内网地址，
Gitea 会直接报 `redirect_uri` 不匹配。


### `convert` 产物

| 输入 | 产物 |
| --- | --- |
| `.PcbDoc` | `board.json`、`bom.xlsx`、`manifest.json` |
| `.SchDoc` | `schematic.json`、`schematic.svg`、`manifest.json` |

同一输入两次转换的产物**字节一致**（固定排序、固定 Excel 文档时间戳、zip 条目时间归零），
这是后续按 `sha256` 做版本差异的前提。

`schematic.json` 的结构：`source`（kind / file / sha256 / 图纸尺寸）、
`components`（位号 / 值 / 封装 / 库 / 引脚）、`nets`（电源口 > 端口 > 网络标签 > 自动命名）、
`warnings`。`schematic.svg` 按层输出（导线 / 结点 / 引脚 / 元件 / 标注），
元素带 `data-designator`，前端可直接做联动高亮。

`verify` 是解析器的验收门槛：同一块板的 PcbDoc 与 Altium/华秋导出的坐标文件
逐位号比对（值 + 封装），全部一致才算通过。

## 界面流程

1. 拖入 `.PcbDoc` / `.SchDoc`（或坐标 CSV/XLSX）。载入后拖拽区自动收起，左侧项目列表可随时切换。
2. 页面按屏宽自适应（全宽布局）：宽屏是三个可拖拽区域 —— 左上**板图 / 原理图**、左下 **3D 装配**、右侧 **BOM 表格**；
   两条蓝色分隔线（竖 `┃` 调左右宽度、横 `━` 调板图 / 3D 高度）在交叉处连成十字，中段胶囊状抓手，
   悬停任一条时两条一起点亮发光；拖动即可分配大小，双击恢复默认，比例记在 localStorage。
   ≤1100px 自动切回单列（板图/原理图 → 3D → 表格）。
3. 板图画真实 PCB 几何：板框（`BoardRegions`）+ 丝印（`Tracks6`/`Arcs6`）+ 焊盘（`Pads6`），并按 BOM 行高亮：
   - 点表格行 → 板图上该行的元件变黄/绿并标出位号
   - 点板图上的焊盘 → 自动定位到对应表格行
   - **滚轮缩放（以光标为中心）、按住拖动平移**，`适配` 回到全览
   - `翻面` 切换 Top / Bottom 视图（Bottom 自动镜像，只显示下层丝印与通孔/底层焊盘）
4. 载入 `.SchDoc` 时，左上区域自动切成**原理图**（板图与 3D 无意义，槽位复用，不再占地方）：
   - 导线 / 结点 / 引脚 / 元件框 / 电源口 / 网络标签分层绘制
   - 滚轮缩放、拖动平移、`适配` 复位
   - 点原理图里的元件 → 选中对应 BOM 行；表格悬停 → 原理图对应元件高亮
   - 标题右侧显示 `元件数 · 网络数`，数据来自 `/api/schematic/{id}`
5. 下方 **3D 布局** 内嵌 `/3d`（同一套 three.js + occt-import-js，不重复加载页面）：
   - 表格行选中/悬停 → 3D 中对应器件高亮（琥珀 = 待确认，绿 = 已映射，蓝 = 悬停）
   - 点击 3D 器件 → 选中对应 BOM 行；双击聚焦到该器件
   - 标题栏五个一级操作：`翻面`（顶/底对调）、`适配`（按当前面回到全览，转飞了也能一键回来）、
     `重载`（重新解析）、`新窗口`（独立 `/3d` 页，会 307 到 `/static/3d.html`）、`收起/展开`
   - 首次解析 25 个左右 STEP 需要十几秒，进度显示在面板标题；三角化结果按 `sha256(STEP)`
     存入浏览器 IndexedDB，切换项目 / 刷新后直接复用，命中时连 OCCT WASM 都不加载
   - 原理图项目不会改动 3D iframe：切到 `.SchDoc` 再切回板子，已加载的 3D 场景保持原样
6. 表格里每行是一组相同 `值 + 封装 + 耐压` 的元件
7. 在“料号”列输入料号回车 → 写入别名，下次自动带出；没有料号点 **新建**（会带出值/封装/耐压）
8. **导出 Excel**：3 个 sheet
   - `生产BOM`：位号（自动合并成 R1-R3,R7）、数量、料号、状态
   - `采购汇总`：需求 / 库存 / 建议采购 / 单价 / 金额
   - `待确认`：还没有料号的行 + 最接近的候选

## 本地字体（SchDoc）

原理图中带有 `FONTID` 的纸面文字、位号、值和标签会保留源文件字体族、字号、位置/旋转与可见状态。Web 不依赖浏览器所在机器的字体：把已获授权的 `.ttf`、`.otf`、`.woff`、`.woff2` 或 `.ttc` 文件放入 `apps/local/static/fonts/`，服务会自动读取字体元数据并为 TTC 集合登记各 face。字体缺失时，当前原理图面板会提示源文件请求的字体族。

特殊情况下可在同目录创建可选 `fonts.json` 覆盖字体族名、样式或字重（文件名须与目录内文件完全一致）：

```json
{
  "fonts": [
    {"family": "Arial", "file": "Arial-Regular.woff2", "style": "normal", "weight": "400"},
    {"family": "Times New Roman", "file": "TimesNewRoman-Regular.woff2", "style": "normal", "weight": "400"}
  ]
}
```

字体文件的分发须符合相应字体许可证。项目不自动下载或重新分发系统字体。`PcbDoc` 文字对象的绘图属性尚未解析，板图标签字体目前不属于这套 SchDoc 字体处理范围。

## 原理图（SchDoc）

```
OLE 容器
  └─ FileHeader: [u32 长度][|KEY=VALUE…| 结尾 NUL] × N   (latin-1)
       RECORD=1  元件        RECORD=2  引脚
       RECORD=17 电源口      RECORD=18 端口     RECORD=25 网络标签
       RECORD=27 导线        RECORD=29 结点     RECORD=31 图纸设置
       RECORD=34 位号        RECORD=41 参数
```

- 键名统一大写后再取值（不同 Altium 版本 `Location.X` / `LOCATION.X` 混用）
- 子记录通过 `OwnerIndex` 归属元件；实现按 `+1` / `+0` / `-1` 依次探测，仍失败则顺序回退
- 引脚热端 = `Location + PinLength × dir(PinConglomerate & 3)`，方向 `0=+x 1=+y 2=-x 3=-y`
- 坐标单位 10 mil（`0.254 mm`），支持 `_FRAC` 子单位（1/100000）
- 连通性用几何并查集：导线顶点、引脚热端、结点、标签锚点；点落在导线上也算连接
- 网络命名优先级：电源口 > 端口 > 网络标签 > `N$0001`；成员按位号自然排序
- 同一 `designator` 的多个 `RECORD=1` 实例（多 part）按位号合并，不拆成两个元件

**已知不解析**：总线、层次图纸（Sheet Symbol / Sheet Entry）、隐藏引脚、实现（Footprint）模型引用。
这些会写进 `warnings`，不会静默丢数据。`SchDoc` 也不含 DNP / 装配变体信息（在 `.PrjPcb`）。

## 归一化规则

| 输入 | 归一化结果 |
| --- | --- |
| `100nF` `0.1uF` `104` `100NF` `.1µF` | `100nF` |
| `4K7` `4.7k` | `4.7K` |
| `1 KOHMS` `10 Ohms` | `1K` / `10R` |
| `104`（电阻） | `100K` |
| `10uF/25V` `10uF 25V` | 值 `10uF` + 耐压 `25V` |
| `YAGEO RES 0402_1005` `0603 (1608)` | `0402` / `0603` |
| `SOP-8` `SSOP16` `QFN-24 4x4` | `SOIC-8` / `SSOP-16` / `QFN-24` |

分组 key = `类型 | 值 | 封装 | 耐压 | 精度 | 功率`，所以 `10uF/16V` 与 `10uF/25V`、
`100nF 0402` 与 `100nF 0603` 都不会被合并错。

## 匹配优先级

1. **规则**（正则，优先级可调）—— 适合“所有 10uF/0603 都用这个料号”
2. **别名**（历史上确认过的映射）—— 完全命中，置信度 100
3. **模糊匹配**（rapidfuzz，默认阈值 88）—— 只作建议，不自动确认

## 目录结构

```
bomkit/                 纯 Python 核心（无 UI / 无 Web 依赖，可直接移植到服务端）
  parsers/pcbdoc.py     PcbDoc OLE 解析（Components6 + Texts6 + WideStrings6）
  parsers/schematic.py  SchDoc OLE 解析（记录流 → 元件/引脚/网络 → JSON/SVG）
  parsers/pads.py       Pads6 焊盘（板图）
  parsers/graphics.py   BoardRegions 板框 + Tracks6/Arcs6 丝印
  parsers/pnp.py        坐标文件解析
  artifacts.py          确定性产物（project.json / bom.xlsx）与 bundle.zip 打包
  designs.py            项目 / 版本 / 快照 / 分享的流水线（唯一写入口）
  sharing.py            slug、提取码、有效期、访问 Cookie
  auth.py               Gitea OAuth + HMAC 签名 session（无新增依赖）
  normalize.py          值 / 封装 / 耐压 归一化
  mapping.py            分组 + 规则/别名/模糊匹配
  db.py                 SQLAlchemy 模型（SQLite ↔ Postgres 同一套）
  service.py            编排
  excel.py              3 sheet 输出（`deterministic=True` 时字节可复现）
apps/
  cli.py                parse / verify / parts / seed / serve / convert / offline / schdoc
  local/server.py       FastAPI
  local/offline.py      单文件离线页（内联数据 + three.js + 离线 3D）
  local/static/         单文件前端（BOM 页面 + 3D 页面 + 内置 three/occt 依赖）
    gerber/             `/gerber` 钢网拼版编辑器的构建产物（由 web/panel 生成）
web/
  src/                  工作台 SPA（Vue 3）→ 构建到 apps/local/static/admin
  panel/                钢网拼版编辑器（Vue 3，独立小应用）→ 构建到 apps/local/static/gerber
tests/                  用例：PcbDoc 与坐标文件对拍、SchDoc 记录/连通性/API、产物确定性、设计与分享
```

## PcbDoc 解析要点（已实测）

| 内容 | 位置 |
| --- | --- |
| 封装、层、坐标、旋转、库链接 | `Components6/Data`：`[i32 size][|KEY=VALUE| cp1252 C-string]` |
| 位号 / Comment（值） | `Texts6/Data`：`[u8 id][i32 size][payload][i32 size][pascal string]`，payload 偏移 40/41 是 comment/designator 标志，偏移 7 是元件索引 |
| 焊盘（板图） | `Pads6/Data`：3 个 string block + 3 个 block；主块偏移 7=元件索引、13/17=坐标、21/25=尺寸、45=钻孔、49=形状、52=旋转、60=镀铜 |
| 板框 | `BoardRegions/Data`：`[u8 id][i32 size][payload]`，payload 里嵌参数块后是 `vertexCount u32` + N 个 `(f64 x, f64 y)` |
| 丝印 | `Tracks6/Data`（层@0、x1@13…width@29）、`Arcs6/Data`（圆心@13、半径@21、起止角@25/@33） |
| 中文等非 Latin 文本 | `WideStrings6/Data`：`[u32 index][u32 byteLen][utf-16le]`，payload 偏移 115 引用 |
| 坐标单位 | 1 内部单位 = 1/10000 mil，`mm = raw / 10000 × 0.0254` |

板图只取丝印层（33=Top Overlay、34=Bottom Overlay），网络/走线/铜皮不参与渲染。

焊盘形状（`TPadShape`）：`1` 圆/椭圆 —— **w==h 画圆，w!=h 画胶囊（oblong）**；
`2` 矩形；`3` 八边形；`9` 圆角矩形。钻孔（偏移 45）> 0 时在焊盘中心画深色孔。

验证结果：`tests/data/spi.pcbdoc`（50 元件）与独立实现（AltiumSharp 导出的模型）
逐位号比对，**50/50 的值与封装完全一致**。

## 已知限制

- `COMPONENTKIND` 的取值映射（0=Standard、1=Mechanical…）按常见约定处理，默认排除
  1/2/3/4/5；如果你的板子编号不同，可在 `parsers/pcbdoc.py` 的 `DEFAULT_EXCLUDED_KINDS` 调整。
- 变体（Variants）不解析：PcbDoc 里没有 Vault/工程级变体信息，需要读 `.PrjPcb`（后续）。
- 同名网络/机械孔不会出现在 `Components6`，无需额外过滤。
- 目前只读元件级信息；`Pads6`（焊盘数、开口面积）未解析。

## 为什么没有直接用 InteractiveHtmlBom

IBOM 的**生成**环节绑定 KiCad 的 `pcbnew` Python API（它的 README 也写明 EasyEDA/Eagle/Fusion/Allegro
是"先转 KiCad 再用"），产物是一个自包含 HTML。它有两条借用路线：

| 路线 | 做法 | 代价 |
| --- | --- | --- |
| A. 复用自己的板图（已实现） | 读 `Pads6` 自己画 Canvas，BOM 行 ↔ 焊盘双向高亮 | 已包含在工具里，零外部依赖 |
| B. 生成 IBOM 的 pcbdata | 它公开了 `DATAFORMAT.md`，我们自己产出 JSON，再套它的前端 | 需要焊盘/丝印/装配层全几何 + 版本跟随；IBOM 擅长的是手工焊接指引，不是 BOM 映射 |
| C. Docker 里装 KiCad 转换 | `pcbdoc → kicad_pcb → IBOM` | 镜像 +1GB、需 `pcbnew` 绑定、Altium 转换有保真度风险 |

**A 已经覆盖"哪个位号在哪、属于哪一行"的核心需求**；如果以后要给产线出 IBOM 那种带丝印/
网络高亮的焊接指引，再走 B（复用它的前端），不必装 KiCad。

## 3D 模型（STEP 提取）

PcbDoc 会把元件的 3D 模型**直接嵌在文件里**：

| 内容 | 位置 |
| --- | --- |
| 模型索引 | `Models/Data`：`[i32 size][|K=V|]`，含 `NAME=xxx.STEP`、`ID={GUID}`、`CHECKSUM`、`ROTX/ROTY/ROTZ/DZ`（`DZ` 是 1/10000 mil 的内部单位） |
| STEP 正文 | `Models/<n>`：**zlib 压缩**的 STEP（AP203/AP214，`ISO-10303-21`） |
| 摆放（每处一份） | `ComponentBodies6/Data`：`MODELID`、`MODEL.2D.X/Y/ROTATION`、`MODEL.3D.ROTX/ROTY/ROTZ/DZ`（mil）、`IDENTIFIER`（模型名去扩展名的 ASCII 码）、`STANDOFFHEIGHT`/`OVERALLHEIGHT`、`BODYCOLOR3D`、`BODYOPACITY3D`、`BODYPROJECTION`、`MODEL.MODELTYPE`、`MODEL.EXTRUDED.MINZ/MAXZ` |
| 2D 轮廓（每处一份） | `ComponentBodies6/Data` 参数串之后：`[u32 顶点数][顶点数 × (f64 x, f64 y)]`，内部单位、**绝对板坐标** |

一个元件可以有**多个 body**：多 P 端子（`KF2EDGK-7P`）是 7 个单 P STEP 叠出来，电感
（`L1008`）是「本体 + 2 个电极 + 2 片端子」5 个 body。所以渲染单位是 body，不是元件。

`MODEL.MODELTYPE=0` 的 body **不是 STEP**，是 Altium 里按 2D 轮廓拉伸出来的实体：
`MODEL.NAME` 为空、`MODEL.EXTRUDED.MINZ/MAXZ` 给出 Z 范围（mil），轮廓就是几何本身。
`L1008` 电感、`2512` 电感都属于这种，按 STEP 找当然找不到（以前只能画一个灰盒子）。
这类 body 的 `IDENTIFIER` 往往**是空的**（没有模型名），不能因为 `IDENTIFIER` 为空就把整条记录丢掉
——否则 `EspRTU.PcbDoc` 的 `Si3404(U10)`、`L_4/L_5` 电感、`LED0603` 都会凭空消失。

body 的 `MODEL.3D.ROTZ` 是**绝对**的平面朝向（Altium 在每次摆放时改写它，所以不要再叠加
`Components6` 的 `ROTATION`）；`MODEL.3D.DZ` 是模型在 Z 方向的偏移，单位 mil；
校验方法：`dz + 模型旋转后的最低点 == STANDOFFHEIGHT`（`tcpLock.PcbDoc` 的 102 个 body 全部精确吻合）。

**摆放基准是 body 的 2D 轮廓中心，不是元件原点**：STEP 的坐标原点不一定在模型中心
（单 P 端子的原点在锁扣端，偏移 5.15 mm），而 `MODEL.2D.X/Y` 有时是陈旧的
（`tcpLock` 的 Y1 晶振偏 2 mm）。把「模型旋转后的包围盒中心」对齐到「body 轮廓中心」
可以同时满足这两类文件，并且和 Altium 的显示结果一致（`EspRTU` 的 7P 端子逐 P 对位、
ESP32-WROVER 天线端多出的 7.4 mm 也不会把模组推歪）。

界面上「3D 模型」页会把它们列出来（按使用次数排序，显示使用元件、压缩后大小、模型旋转），
支持单个下载和 **下载全部 STEP (zip)**，zip 里带 `manifest.json`（文件 ↔ 模型 ↔ 位号 ↔ 旋转）。

这一步只是 `zlib.decompress`，**不需要 CAD 内核**；`tcpLock.PcbDoc` 实测 48 个模型、压缩 7.0 MB、
解压后 36 MB，25 个被元件引用。

## 3D 预览（浏览器端）

`/3d?token=...`（BOM 页面「3D 模型」→「打开 3D 预览」；该路径会 307 到
`/static/3d.html?token=...`，因为页面里的脚本用相对路径引用同目录资源）：

- **three.js** 渲染 + **occt-import-js（OCCT 编译成 WASM）** 在浏览器里直接解析 STEP，服务端不需要 CAD 内核
- 板框按 `BoardRegions` 拉伸成实体（带 cut-out 支持）；板面/板边颜色取自 `Board6/Data` 的
  `CFG3D.TOPSOLDERMASKCOLOR` / `BOARDCORECOLOR`（`0xBBGGRR`，`0` 就是黑色），背景用 `CFG3D.WORKSPACECOLOR`
- 焊盘按 `Pads6` 画成金色/镀锡铜环，贯穿孔两侧都有
- 渲染单位是 **3D body**：位置取该 body 的 2D 轮廓中心，
  姿态取它自己的 `MODEL.3D.ROTX/ROTY/ROTZ`（先 X 后 Y 再 Z，即 `Rz·Ry·Rx`，因为多数 STEP 是 Y 朝上、
  靠 `ROTX=90` 立起来）
- 拉伸体（`MODEL.MODELTYPE=0`）按 body 轮廓 + `EXTRUDED.MINZ/MAXZ` 生成棱柱，颜色取 `BODYCOLOR3D`（sRGB）
- 高度：`MODEL.3D.DZ` 按 mil 换算成 mm 后叠加；底面元件整体绕 X 翻 180°（`X→X, Y→-Y, Z→-Z`），
  Z 原点换算到板底 `-(板厚 + DZ)`，与 Altium 的 `STANDOFFHEIGHT` 一致
- 颜色：先看 occt 的 `brep_faces`，缺色时用自己解析的 `STYLED_ITEM → SURFACE_STYLE_* → COLOUR_RGB`
  （按实体面数匹配 OCCT 网格，避免混色）
- 顶视 / 底视 / 等轴测、线框、自动旋转、双击聚焦元件
- 没有 3D body 的元件默认**不显示**；点「占位盒」按钮才用焊盘范围 + `HEIGHT` 画灰盒，
  基准点 / 丝印 / LOGO（`FIDUCIAL*`、`Mark*`、`SIGN*`、机械/图形 kind）永不生成

首次加载要把 25 个 STEP 转成网格（本机实测约 20 秒）；网格按 STEP 的 `sha256`
缓存在浏览器 IndexedDB（`bom3d-mesh-cache`），之后切换项目、重开页面或重新上传同一块板
都直接复用，缓存全命中时不会加载 OCCT WASM。

依赖是**本地内置**的（`apps/local/static/vendor/`）：`three.module.js`、`OrbitControls.js`、
`occt-import-js.js` + `.wasm`（7.7 MB），不联网也能用。

## Gerber 钢网拼版（`/gerber`）

把 Altium / KiCad 导出的 GTP / GBP 锡膏层放进一块固定尺寸的钢网里排版，然后导出合并后的
Gerber / DXF / 报告。**纯前端本地计算，文件不上传、不进数据库、不记历史**，所以它和 `/`、`/3d`
一样是公开工具页。

源码在 `web/panel/`（Vue 3 + TypeScript + Canvas 2D），是一个独立小应用：

```powershell
cd web/panel
npm install
npm run build      # → apps/local/static/gerber/（入口 + assets）
```

产物入仓，所以纯 Python 部署（`python main.py`）不需要 Node 也能打开 `/gerber`；
Docker 镜像里会重新构建一次，详见 `Dockerfile` 的 `gerber` 构建阶段。

- 可用区：钢网外框每边至少预留 **10 mm**（总尺寸减 2 cm），可调到 15 mm
- 板间距：默认 10 mm，可调到 20 mm
- 全部按顶面处理：GTP / GBP 导入后不镜像
- 自动排料（MaxRects）+ 填满钢网 + 框选删除图元
- 主题与首页共用 `localStorage['bom.theme']`，两个工具之间切换不会跳色

它与首页是**平级的两个工具**，不是首页里的一个标签页。以后工具变多时，`/` 可以退化成
「工具选择」页，各自仍按自己的路径进来（`/pcb`、`/gerber` …）。

## Docker / 部署

本地服务支持两种启动方式：

```powershell
.\.venv\Scripts\python.exe main.py
```

或使用 Docker Compose：

```powershell
docker compose up --build
```

Gitea workflow 只负责构建并推送镜像，不会自动重启服务器。默认镜像名为
`git.lotenall.com/python/bom-tool`，同时推送 commit SHA 标签与 `latest`。
服务器上手动维护 compose 文件，先 `docker login`，再执行
`docker compose pull` 与 `docker compose up -d`。详细说明见 [DEPLOYMENT.md](./DEPLOYMENT.md)。

- 只换 `LAYOUT_DB_URL=postgresql+psycopg://...`，`db.py` 无需改动
- FastAPI 路由即为最终接口，pure-admin 直接调用 `/api/*`
- 需要异步任务/多用户时，把 `_BOARDS` 内存缓存换成 Redis + 数据库即可
