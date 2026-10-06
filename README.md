# 未定之书 · ProjectOC

基于 Django REST Framework + Vue 3 的单人、本地 OC 世界观设定工作台。

## 启动

### 本地开发

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
python backend/manage.py migrate
python backend/manage.py runserver
cd frontend && npm install && npm run dev
```

没有 `OPENAI_API_KEY` 时，后端使用离线规则演示模式：它仍然会把用户输入放入提案，不会写入正式实体。

Neo4j 只用于高级图谱投影，不是事实源；Compose 会等待 Neo4j healthcheck 后再启动后端。要运行真实联调测试：

```bash
docker compose exec -T -e RUN_NEO4J_INTEGRATION=1 backend python manage.py test tests.test_neo4j_integration
```

### Docker Compose

```bash
cp .env.example .env
# 如需上游 AI，再编辑 .env 写入 OPENAI_API_KEY
# .env 只保存在本地，禁止提交
 docker compose up --build
```

- 前端：`http://localhost:${FRONTEND_PORT:-5173}`
- 后端健康检查：`http://localhost:${BACKEND_PORT:-8000}/health/`
- API：`http://localhost:8000/api/v1/`

## 当前实现范围

- 无限画布：TLDraw React 适配器挂载到 Vue，支持 Markdown、LaTeX、Mermaid、实体草稿；TLDraw 原生手绘、箭头、便签工具保留。
- AI 产婆对话：SSE 流式回复，OpenAI-compatible 服务动态模型列表；离线 fallback；AI 内容先进入提案。
- 审核流程：提案编辑、关系选择、冲突提示、提交前 diff、幂等提交；数据库确认后排队 Git 同步。
- 正式世界观：实体、关系、出链、反向链接、Cytoscape 图谱、Git 历史/diff、失败任务重试。
- M6 协作：账户与 owner/editor/reader 权限、邀请、画布工作分支与合并、Neo4j 可重建投影，以及自托管 WebSocket 协作。所有画布统一使用官方 `tldraw-sync-v2`（`TLSocketRoom` / `SQLiteSyncStorage`）；`records-v1` 已废止，不提供实时连接、写入或降级。
- Agent 长期记忆：记忆按 workspace/branch 隔离，正式事实与待审核草稿分层；用户可编辑工作笔记、归档/恢复非阻塞问题，并通过版本号和审计记录追踪变更。
- 分支合并审核：支持三方字段差异预览、非冲突字段自动合并、逐字段选择 main/branch，以及用户编辑最终字段值；AI 合并建议只作为可审核建议，不会自动写入。
- 数据库与 Git：PostgreSQL 是事实源；世界观内容存入 `world_repos/<slug>-<id>/`，应用源码与内容仓库分离。
- Git 恢复：可用 `python backend/manage.py reconcile_commit_jobs --workspace <workspace-id>` 检查 marker commit；加 `--retry` 会重放没有 marker 的可恢复任务；`/api/v1/workspaces/{id}/git/status/` 提供分支 ref、基线和待处理任务状态。版本与维护页现在会汇总 Git、同步任务和 Neo4j 投影健康状态，并支持请求投影重建。
- 高级图谱：图谱详情支持最短路径和影响范围分析，优先使用 Neo4j、不可用时明确回退 PostgreSQL；结果带有来源标记。
- Neo4j 联调：`backend/tests/test_neo4j_integration.py` 提供真实 Neo4j 的 branch 投影、路径、影响分析和幂等 rebuild 测试；在 Compose 网络内以 `RUN_NEO4J_INTEGRATION=1` 显式运行。

## 当前限制与生产前置条件

- 时间系统、时间切片人物生命周期已在后端完成；现在支持显式有向时间体系换算、可组合精确转换、时间体系编辑器、可视化时间轴、事件/人物生命周期条带、范围滑块和切片游标。时间体系新增确定性的 variable-months 日历规则、闰日、纪元和无零年显示，并随 Git 快照导出；TimelinePanel 支持 commit/snapshot 双版本只读 Diff，展示事件新增/删除/移动、参与者、生命周期、关系有效期和时间体系 warning，并可定位证据。
- 同步统一使用 `tldraw-sync-v2`。SQLite 保存同步协议 journal/cache，PostgreSQL durable snapshot/event 和 document clock 是领域恢复事实源；Redis 用于房间租约，防止同一房间双主。光标和选区由官方 presence 管理。旧快照及尚未纳入快照的历史操作日志仅用于一次性读取迁移；日志缺失会停止迁移。
- 自托管协作只适用于私有网络。生产必须使用 HTTPS/WSS 反向代理、强随机且独立的 `SYNC_TICKET_SECRET` / `SYNC_INTERNAL_SECRET`、数据库/Git/快照备份，以及有效 TLDraw 生产许可。不要使用公开演示同步服务器。
- Compose 默认端口绑定到 `127.0.0.1`；部署到私有网络前仍需配置 `DEBUG=0`、允许主机、可信代理和安全 Cookie。实体和关系正式写入仅允许通过审核提案提交流程。

### 私有网络协作部署示例

`deploy/` 提供 HTTPS/WSS Nginx 配置和生产 Compose override。所有画布使用 `tldraw-sync-v2`，无协议选择开关。生产启用前仍必须通过有效 TLDraw license、HTTPS/WSS、备份恢复和 credentialed 多浏览器验收。

### Durable outbox worker

Git 同步和 Neo4j 投影现在由可重试的持久化 outbox worker 处理。Compose 会启动独立的 `outbox-worker` 服务；任务使用 PostgreSQL lease 防止重复领取，并按指数退避重试：

```bash
# 手动处理一批任务
python backend/manage.py run_outbox_worker --once

# 私有部署中持续运行（Compose 已自动配置）
python backend/manage.py run_outbox_worker --loop --interval 5 --limit 20
```

任务达到 `OUTBOX_MAX_ATTEMPTS` 后会保留失败状态，必须由维护人员检查错误并显式重试；数据库确认内容不会因为 Git 或 Neo4j 暂时不可用而回滚。

### records-v1 退役与升级

前后端需要一起更新，并刷新旧浏览器页面。旧协议 WebSocket 请求返回 HTTP 410；旧 `/sync-ops/` 写入和带 `protocol=records-v1` 的 `/sync-state/` 写入同样返回 410。历史数据库表、快照和操作日志保留，内部 `/sync-ops/` GET 仅供迁移读取。冻结的 `legacy-record-migration.js` 只还原升级前的历史操作，不参与新编辑。

浏览器 `oc:sync-pending-ops:<canvas-id>` 中未发送的旧操作保持原样，画布提供导出入口，需要人工核对；不会清空或自动重放到官方房间。本地完整快照仍可导出，不再通过 REST 覆盖共享画布。旧 `TLDRAW_SYNC_OFFICIAL_ENABLED` / `VITE_TLDRAW_OFFICIAL_SYNC` 配置不再选择协议。

实时连接状态不代表 PostgreSQL 已落盘。`/health` 中的 `pending_persistence` 和 `last_error` 用于检查持久化进度，备份和重启前应确认队列清空。请勿在升级前删除旧日志或本地备份。
