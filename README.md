# 未定之书 · ProjectOC

**把角色、世界规则与故事灵感，整理成可审核、可追溯的世界观。**

ProjectOC 是面向原创角色（Original Character，OC）创作者的自托管工作台。你可以在无限画布上整理设定，通过 AI 对话完善想法，再把审核后的草稿纳入正式世界观，与受邀成员一起编辑。

[快速启动](#快速启动) · [功能](#功能) · [文档](#文档) · [参与开发](CONTRIBUTING.md) · [MIT 许可](LICENSE)

> 当前处于开发阶段，适用于本地使用和私有网络协作。画布统一使用官方 TLDraw 同步；旧 `records-v1` 已退役。正式部署仍需完成许可、HTTPS/WSS 和备份恢复验收。

## 工作方式

```text
画布整理灵感 → AI 对话与草稿提案 → 人工编辑和审核 → 正式实体与关系
                                                       ↓
                                              Git 历史 · 图谱 · 时间轴
```

AI 生成内容先进入提案，审核后才成为正式设定。PostgreSQL 保存正式数据，Git 记录世界观内容历史，Neo4j 提供可重建的图谱投影。

## 功能

| 功能 | 可以做什么 |
| --- | --- |
| 无限画布 | 整理 Markdown、LaTeX、Mermaid 和实体草稿，使用手绘、箭头与便签 |
| AI 对话 | 使用兼容 OpenAI 接口的模型服务，流式回复、整理提案和维护工作记忆 |
| 提案审核 | 编辑实体与关系、查看提交前差异，确认后纳入正式世界观 |
| 知识图谱 | 查看关系、出链和反向链接，分析最短路径与影响范围 |
| 时间体系 | 定义日历和时间换算，查看事件、人物生命周期及时间切片 |
| 分支与历史 | 在工作分支中探索设定，审核三方合并差异，查看 Git 历史与时间轴版本差异 |
| 私有协作 | 邀请 owner/editor/reader 成员，通过官方 TLDraw 同步共享画布 |
| 维护与恢复 | 查看同步、Git 和投影任务状态，重试失败任务，备份与恢复数据 |

没有配置模型 API 密钥时，AI 对话使用离线规则演示模式。它用于体验提案流程，不具备在线模型的生成能力。

## 快速启动

需要安装 Docker 和支持 `--wait` 的 Docker Compose。以下命令在仓库根目录执行：

```bash
git clone https://github.com/HeDaas-Code/ProjectOC.git
cd ProjectOC

# 已有 .env 时保留原配置；按需填写模型服务和本地端口。
[ -f .env ] || cp .env.example .env

docker compose up -d --build --wait --wait-timeout 180
```

首次构建需要下载镜像与依赖。Compose 会启动 PostgreSQL、Redis、Neo4j、后端、画布同步服务、前端和后台任务 worker。

| 地址 | 用途 |
| --- | --- |
| [localhost:5173](http://localhost:5173/) | 工作台；首次使用创建 Owner，之后通过邀请加入成员 |
| [localhost:8000/health/](http://localhost:8000/health/) | 后端健康检查 |
| [localhost:8787/health](http://localhost:8787/health) | 画布同步协议、房间与持久化状态 |

端口默认只绑定 `127.0.0.1`。可通过 `.env` 中的 `FRONTEND_PORT`、`BACKEND_PORT` 和 `SYNC_PORT` 调整；前端端口变化时也要更新 CORS、CSRF 和 WebSocket origin 配置，同步端口变化时更新 `SYNC_SERVICE_URL`。

常用命令：

```bash
docker compose ps
docker compose logs --tail=100 backend sync-service outbox-worker

# 更新镜像并重新启动
docker compose up -d --build --wait --wait-timeout 180

# 停止服务，保留数据库卷和世界观文件
docker compose down
```

`docker compose down -v` 会删除数据库卷，不用于普通停止或升级。世界观内容保存在 `world_repos/`，请与数据库一起备份。

## 架构与目录

| 部分 | 技术与职责 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Pinia、Vite；通过 React 适配 TLDraw |
| 后端 | Django REST Framework；账户权限、世界观数据、提案审核与 AI 编排 |
| 同步服务 | Node.js、WebSocket、官方 `TLSocketRoom` / `SQLiteSyncStorage` |
| 数据与任务 | PostgreSQL、Redis 房间租约、Git 内容仓库、Neo4j 图谱投影、持久化 outbox worker |

```text
ProjectOC/
├── backend/        # Django 应用、迁移与测试
├── frontend/       # Vue 工作台、TLDraw 适配与浏览器测试
├── sync-service/   # 官方画布同步与历史数据迁移
├── deploy/         # 私有部署、备份恢复与 staging 脚本
├── docs/           # 当前指南与归档设计文档
└── world_repos/    # 本地世界观内容仓库，内容不提交到应用源码仓库
```

架构和一致性边界见 [架构说明](docs/architecture.md)。

## 当前限制

- 实时连接成功不代表 PostgreSQL 已落盘；升级和备份前应检查同步服务的 `pending_persistence` 与 `last_error`。
- 画布自动同步不等于所有并发文本修改都能无损合并。长期离线、强制终止进程和数据库故障等场景仍需专项验收。
- 旧协议仅保留历史快照与操作日志的只读迁移能力，不提供实时连接或降级；旧浏览器未发送的操作需导出后人工核对。
- 默认 Compose 是本地开发配置。私有部署需使用 HTTPS/WSS、独立密钥、数据库与 Git 备份，以及有效的 TLDraw 生产许可。

## 文档

| 文档 | 内容 |
| --- | --- |
| [文档索引](docs/README.md) | 按使用、开发、部署和历史资料查找 |
| [开发指南](docs/development.md) | 本地开发、依赖安装和测试命令 |
| [架构说明](docs/architecture.md) | 模块职责、数据源与同步流程 |
| [部署与运维](deploy/README.md) | HTTPS/WSS、配置、备份与恢复 |
| [旧协议升级](docs/migration.md) | `records-v1` 退役、历史数据与本地备份 |
| [同步服务](sync-service/README.md) | 官方房间协议、租约和健康检查 |

## 许可

项目源码使用 [MIT License](LICENSE)。第三方依赖、模型服务与 TLDraw 生产许可按各自条款使用。
