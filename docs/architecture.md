# 架构说明

[返回文档索引](README.md)

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `backend/apps/accounts` | 会话认证、工作区成员、邀请与角色权限 |
| `backend/apps/core` | 工作区、分支、实体关系、时间体系、图谱和维护任务 |
| `backend/apps/canvas` | 暂存画布、提案、审核提交、快照与同步事件 |
| `backend/apps/ai_agent` | 模型服务、对话、分层记忆、分析与合并建议 |
| `backend/apps/version_control` | 世界观内容导出、Git 历史与同步恢复 |
| `frontend/src` | Vue 界面、Pinia 状态及 React/TLDraw 画布适配 |
| `sync-service/src` | 官方同步房间、房间租约、持久化与历史导入 |

## 正式设定与草稿

画布、AI 提案和工作笔记属于创作阶段。正式实体与关系通过审核提交流程写入数据库；AI 建议不自动成为正式事实。

提交前预览生成摘要，确认时校验摘要和幂等键。数据库事务确认数据后，Git 同步与 Neo4j 投影通过持久化 outbox worker 处理。外部同步失败不会回滚已确认的正式内容，任务保留错误并按策略重试。

## 数据源

- **PostgreSQL**：部署中的领域事实源，保存账户、工作区、实体关系、提案、时间数据、画布快照、同步事件和任务。
- **Git**：每个世界观的内容历史与导出，位于 `world_repos/<slug>-<id>/`，独立于应用源码仓库。
- **Neo4j**：可从正式数据重建的图谱投影。高级查询不可用时可回退 PostgreSQL，并标记结果来源。
- **世界观图谱画板**：每个 `workspace/branch` 一张官方 `tldraw-sync-v2` 画布。正式实体以稳定 `entity:<uuid>` 节点投影，正式关系以绑定 `relation:<uuid>` 箭头投影；画板创建的关系先进入 `RelationProposal`，审核提交后才写入正式关系。布局属于画布快照，语义数据仍由 PostgreSQL 正式实体/关系提供。
- **Redis**：官方同步房间的跨实例租约协调，不保存领域事实。
- **同步 SQLite**：官方房间的协议 journal/cache。恢复时与 PostgreSQL 的 clock/hash 对账，必要时回放事件或重建。
- **本地开发 SQLite**：未设置 PostgreSQL `DATABASE_URL` 时的 Django 开发回退，不等同于同步服务的 SQLite。

## 画布同步

```text
Vue 工作台中的 React/TLDraw
           │ useSync + WebSocket
           ▼
官方 TLSocketRoom ─── Redis 房间租约
           │
           ▼
SQLiteSyncStorage
           │ 提交回调与重试队列
           ▼
Django 内部接口 → PostgreSQL 快照与 CanvasSyncEvent
```

每个 `workspace / branch / canvas` 只允许一个活跃官方房间。租约丢失时关闭房间，客户端重连并重新取得票据。无 Redis 时租约仅在当前进程生效，适用于单进程开发。

浏览器使用签名票据接入房间；reader 在服务端以只读会话连接。Django 内部同步接口使用独立服务密钥，不接受浏览器会话代替内部认证。

实时同步与 PostgreSQL checkpoint 是两个阶段。房间健康检查中的 `pending_persistence`、`durable_document_clock`、`last_error` 和 reconciliation 状态用于判断持久化进度；前端“已连接”仅表示同步连接状态。

旧 `records-v1` 不再处理新编辑。冻结的历史解码器仅用于导入尚未纳入快照的旧日志，详见 [升级指南](migration.md)。

## AI 与时间模型

AI 记忆按工作区和分支隔离，将正式事实、工作笔记、待审核草稿与问题区分。手工编辑通过 revision 和审计记录追踪；合并建议需要人工审核。

时间体系支持日历规则、纪元、时间换算、事件参与者、人物生命周期及关系有效期。时间轴版本差异从 Git commit 或快照读取，展示对应证据。

## 当前边界

默认 Django 开发服务器和 Compose 配置用于本地开发。私有网络部署配置见 [部署指南](../deploy/README.md)。官方同步不意味着字符级无损合并；数据库故障与强制进程终止后的恢复，需要独立验收。
