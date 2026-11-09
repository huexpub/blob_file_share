# TODO

已完成：

- ~~Gitea OAuth 登录~~（`BFS_GITEA_URL` / `_CLIENT_ID` / `_CLIENT_SECRET`）
- ~~CI 上传 API~~：`POST /api/shares/batch`，多文件，返回一条可直接转发的分享消息
- ~~OAuth 用户自建 CI token~~：明文只显示一次，可撤销，归属到本人
- ~~历史入库~~：OAuth 模式下按账户记录，删除历史即焚毁内容

待定：

- 长日志按级别过滤 / 折叠
- 代码行号与「复制代码」按钮
- 反代后的可信代理白名单（当前读 `X-Forwarded-For` 第一段，未校验来源）
