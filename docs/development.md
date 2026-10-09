# 开发指南

[返回文档索引](README.md)

## 环境

- Python 3.12（与后端镜像一致）。
- Node.js 22；同步服务使用 `node:sqlite`，本地应使用支持该模块的 Node 22 版本。
- Git，以及需要容器时使用的 Docker Compose。

依赖声明见 [后端 requirements](../backend/requirements.txt)、[前端 package.json](../frontend/package.json) 和 [同步服务 package.json](../sync-service/package.json)。前端与同步服务使用各自 lockfile，通过 `npm ci` 安装。

## 推荐：Compose 启动

按 [README 快速启动](../README.md#快速启动) 启动完整服务。后端源码通过 bind mount 加载；前端与同步服务代码复制进镜像，修改后需要重新构建对应服务：

```bash
docker compose up -d --build frontend sync-service
docker compose logs --tail=100 backend sync-service outbox-worker
```

## 无容器开发

在仓库根目录安装依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
npm ci --prefix frontend
npm ci --prefix sync-service
```

以下最小配置使用 Django SQLite 和单进程同步租约，不依赖 Redis 或 Neo4j。不设置模型密钥时使用离线规则演示。

终端一，启动后端：

```bash
. .venv/bin/activate
python backend/manage.py migrate
python backend/manage.py runserver 127.0.0.1:8000
```

终端二，启动同步服务：

```bash
cd sync-service
DJANGO_INTERNAL_URL=http://127.0.0.1:8000 npm run dev
```

终端三，启动前端：

```bash
cd frontend
npm run dev -- --host 127.0.0.1 --port 5173
```

前端默认代理本机 8000 端口。后端与同步服务须使用一致的 `SYNC_TICKET_SECRET`、`SYNC_INTERNAL_SECRET`；最小启动使用各自代码中匹配的开发默认值。不要直接加载容器用 `.env` 后混用 `backend`、`redis` 等 Docker 内部主机名。

嵌套控制台默认开启。迁移或回退旧导航时，可在前端构建环境设置
`VITE_NESTED_CONSOLE=false`；该开关只隐藏只读容器投影，不删除容器、引用、提案或画布数据，重新设置为 `true` 即可恢复。

终端四，处理 Git 和图谱后台任务：

```bash
. .venv/bin/activate
python backend/manage.py run_outbox_worker --loop --interval 5 --limit 20
```

需要完整图谱联调或部署行为时使用 Compose。SQLite 的并发与锁行为不代表 PostgreSQL 部署结果。

## 检查与测试

从仓库根目录执行：

```bash
# 后端单元与回归测试
(cd backend && ../.venv/bin/python -m pytest -q)

# Django 配置和迁移检查
.venv/bin/python backend/manage.py check
.venv/bin/python backend/manage.py makemigrations --check --dry-run

# 前端测试与 TypeScript/Vite 构建
npm test --prefix frontend
npm run build --prefix frontend

# 同步服务测试
npm test --prefix sync-service
```

浏览器协作测试会启动隔离的 Django、同步服务与 Vite，使用根目录 `.venv`。首次安装浏览器后运行：

```bash
cd frontend
npx playwright install chromium
npm run test:e2e
```

测试使用 5175、8124、8788 和 8789 端口，以及 `/tmp/projectoc-playwright.sqlite3` 和 `/tmp/projectoc-playwright-sync`。避免同时运行多套浏览器测试；它会重建自己的测试数据库，不使用开发数据库。

真实 Neo4j 测试需要完整 Compose：

```bash
docker compose exec -T -e RUN_NEO4J_INTEGRATION=1 backend \
  python -m pytest tests/test_neo4j_integration.py -q
```

测试计数会随开发变化。历史验证记录见 [归档实现记录](archive/M6_IMPLEMENTATION_STATUS.md)，不作为当前构建通过的证明。
