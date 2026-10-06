# 私有部署与运维

[返回项目首页](../README.md) · [架构说明](../docs/architecture.md) · [升级指南](../docs/migration.md)

此配置用于私有网络部署。边缘 Nginx 提供 HTTPS、静态前端、Django `/api/` 与官方画布同步 `/rooms/` WebSocket 代理。默认绑定 localhost；若由本机直接提供私网入口，按实际私网接口修改绑定地址。

## 配置与启动

1. 备份已有 `.env`，将 [生产模板](.env.production.example) 复制到根目录 `.env`，填写所有必填空值。已有数据库卷继续使用原数据库密码。
2. 分别生成数据库、Neo4j、Django、同步票据及内部服务密码或密钥。`WORKSPACE_CREDENTIALS_KEY` 用于加密工作区模型凭据，须保持稳定并安全保存。
3. 设置私有域名、匹配的 HTTPS origin，以及浏览器可访问的 `SYNC_SERVICE_URL=wss://你的域名`。
4. 设置 `TLS_CERT_DIR` 为包含 `fullchain.pem` 和 `privkey.pem` 的绝对目录，Docker 用户须可读。
5. 填写有效 `VITE_TLDRAW_LICENSE_KEY`，然后校验并启动：

```bash
# 在仓库根目录执行；生产模板的 COMPOSE_FILE 已包含生产 override。
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
```

也可明确指定配置文件：

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml \
  up -d --build --wait --wait-timeout 180
```

不要提交 `.env` 或证书私钥。更新模板不应覆盖运行环境的密钥，尤其是数据库密码和工作区凭据加密密钥。

## 关键配置

| 配置 | 要求 |
| --- | --- |
| `DJANGO_ALLOWED_HOSTS` | 实际私有域名；保留 localhost/127.0.0.1 供容器健康检查 |
| `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` | 浏览器地址的完整 HTTPS origin |
| `SYNC_ALLOWED_ORIGINS` | 与浏览器 HTTPS origin 一致 |
| `SYNC_SERVICE_URL` | 浏览器可访问的 WSS 地址，不使用 Docker 内部主机名 |
| `SYNC_TICKET_SECRET` / `SYNC_INTERNAL_SECRET` | 两个独立随机值，后端与同步服务保持一致 |
| `TLS_CERT_DIR` | 证书目录；证书应覆盖使用的域名 |
| `HTTPS_BIND` / `HTTP_BIND` | 默认 localhost，仅按实际私网入口调整 |

生产 override 设置 `DJANGO_DEBUG=0`、安全 Cookie 和可信代理头，并隐藏后端与同步服务的直连端口。不要暴露 PostgreSQL、Redis、Neo4j 或内部同步接口。代理须保留 WebSocket Upgrade/Connection 头。

Redis 协调房间租约，PostgreSQL 的快照和同步事件是领域恢复事实源。`records-v1` 已退役，无协议开关或自动降级。

## 健康检查与后台任务

```bash
docker compose logs --tail=100 backend sync-service outbox-worker

# 从容器内读取同步服务健康状态
docker compose exec -T sync-service node -e \
  "fetch('http://127.0.0.1:8787/health').then(r=>r.json()).then(console.log)"

# 手动处理一批 Git 与 Neo4j 任务
docker compose exec -T backend python manage.py run_outbox_worker --once

# 检查某个工作区的 Git 同步；需要重试时再加 --retry
docker compose exec -T backend python manage.py reconcile_commit_jobs \
  --workspace WORKSPACE_UUID
```

将 `WORKSPACE_UUID` 替换为实际工作区 UUID。后端健康检查为 `/health/`。同步健康检查会报告官方协议、房间租约、reconciliation、`pending_persistence`、`durable_document_clock` 和 `last_error`。前端显示“已连接”不等于数据库已落盘；重启、升级和备份前应确认持久化状态。

Compose 自动启动 `outbox-worker`。Git/Neo4j 任务使用数据库租约与指数退避；达到 `OUTBOX_MAX_ATTEMPTS` 后保留失败状态，需要检查原因并显式重试。

## 备份与恢复

备份前停止编辑流量，并等待画布持久化和后台任务完成。脚本备份 PostgreSQL、Django fixture、世界观 Git 仓库与校验清单；Neo4j 可从正式数据重建，未纳入该脚本的事实源备份。

```bash
./deploy/backup.sh

# 指定新的备份目录
./deploy/backup.sh /secure/backup/projectoc-before-upgrade
```

若通过 Compose override 挂载了其他内容目录，必须指明实际路径：

```bash
WORLD_REPOS_ROOT=/actual/path/world_repos ./deploy/backup.sh
```

密钥不会写入备份归档，须单独安全保存。尤其是 `WORKSPACE_CREDENTIALS_KEY`，丢失后无法解密原工作区模型凭据。

恢复会替换数据库内容，应先在隔离环境演练：

```bash
# 确认目标环境与备份校验后执行
CONFIRM_RESTORE=YES ./deploy/restore.sh /secure/backup/projectoc-before-upgrade
```

自定义内容路径恢复时设置 `RESTORE_WORLD_REPOS_ROOT`。恢复脚本保留原目录为带时间戳的 `.pre-restore-*`，迁移数据库后仍需检查画布恢复、Git 状态并重建 Neo4j 投影。

日常停止使用 `docker compose down`；不要在普通升级中添加 `-v` 删除数据卷。

## 隔离 staging 验收

脚本默认使用独立的 `projectoc-staging` Compose 项目与数据卷，生成本地测试证书。它们用于验收，不代表真实生产证书或厂商许可已经通过。

```bash
./deploy/staging-up.sh
./deploy/staging-verify.sh
STAGING_RESTORE_ISOLATED=1 ./deploy/staging-backup-restore.sh
./deploy/staging-failure-drill.sh
./deploy/staging-down.sh
```

报告位于 `.staging-reports/`。运行前确认测试端口、Compose 项目名与目标目录，避免与开发环境端口冲突。生产启用还需有效 TLDraw license、真实 HTTPS/WSS、多浏览器权限与断网重连、备份恢复验收。
