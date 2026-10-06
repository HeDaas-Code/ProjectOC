# ProjectOC 自托管协作部署

这是私有网络部署示例，不是公共 SaaS 配置。边缘 Nginx 负责 HTTPS、静态前端、Django `/api/` 和自托管同步服务 `/rooms/` 的 WebSocket 代理。

## 启动

1. 将 `deploy/.env.production.example` 复制为根目录 `.env`，设置私有 DNS、证书目录和三个彼此独立的随机密钥。
2. 将 `VITE_TLDRAW_LICENSE_KEY` 设置为有效的生产许可；没有许可不要把画布协作部署到生产环境。
3. 确保证书目录包含 `fullchain.pem` 和 `privkey.pem`，并确认 Docker 用户可读。
4. 启动：

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

5. 默认端口只绑定 localhost，适合本机/同主机 TLS 入口；若此 Nginx 主机本身就是私有网络 TLS 边缘，才将 `HTTPS_BIND`/`HTTP_BIND` 改为该私网接口地址，并用防火墙限制来源。不要直接绑定公网接口。不要暴露 Django、PostgreSQL、Redis、Neo4j 或 sync-service 端口。

该示例会强制配置 PostgreSQL、Neo4j、Django、ticket 和内部服务的独立强密码/密钥。`SYNC_SERVICE_URL` 必须是浏览器可访问的 `wss://` 地址，而不是 Docker 内部的 `ws://sync-service:8787`。`SYNC_ALLOWED_ORIGINS` 必须与浏览器地址的 `https://host` 完全一致。`SYNC_REDIS_URL` 应指向私有 Redis；Redis 只负责多实例实时传播，PostgreSQL durable operation log 才是恢复事实源。反向代理必须保留 WebSocket Upgrade/Connection 头。

## 运维前置条件

- `DJANGO_DEBUG=0`、显式 `DJANGO_ALLOWED_HOSTS` 和 HTTPS secure cookies。
- 备份 PostgreSQL、`world_repos/`、画布快照和 Neo4j（Neo4j 可重建，但备份仍建议保留）。
- 轮换三个密钥时同时重启 backend 和 sync-service；旧 ticket 会自然过期。
- 监控 `/health/` 和 sync-service `/health`，并为 Git/图谱投影失败任务安排重试。
- 所有画布统一使用 `tldraw-sync-v2`（官方 `TLSocketRoom` / `SQLiteSyncStorage`），`records-v1` 已退役，无协议开关或异常降级。旧内容通过只读快照/日志导入。生产启用仍必须通过备份恢复、两浏览器断线重连及有效 TLDraw license 验收。

## 备份与恢复

`deploy/backup.sh` 会生成一个带时间戳的备份目录，包含：

- PostgreSQL custom dump（运行时事实源，包含画布快照、操作日志、实体、关系、成员和时间线）；
- 用于检查和 SQLite 恢复演练的 Django fixture；
- `world_repos/` 全部 Git 历史；
- `manifest.json` 与 SHA-256 校验文件。

Neo4j 不作为事实源写入备份；恢复 PostgreSQL 后必须重建 Neo4j projection。备份脚本不会把密钥写入归档。

```bash
./deploy/backup.sh
# 或指定目录
./deploy/backup.sh /secure/backup/projectoc-before-upgrade

# 恢复是破坏性操作，必须显式确认
CONFIRM_RESTORE=YES ./deploy/restore.sh /secure/backup/projectoc-before-upgrade

# staging 脚本会自动选择 projectoc-staging Compose 项目，避免误操作开发环境
STAGING_RESTORE_ISOLATED=1 ./deploy/staging-backup-restore.sh
```

恢复前应停止写入流量，并在隔离环境先做一次恢复演练。`restore.sh` 会把现有 `world_repos/` 改名为带时间戳的 `.pre-restore-*` 目录，以便人工回滚；PostgreSQL 恢复前必须确认备份校验通过。
