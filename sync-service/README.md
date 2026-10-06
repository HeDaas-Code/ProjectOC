# ProjectOC 自托管画布同步

仅提供 `tldraw-sync-v2`，使用官方 `TLSocketRoom` / `SQLiteSyncStorage` 管理画布记录、tombstone、clock、schema 和同步事务。`records-v1` 已退役，其 WebSocket 入口返回 410，无降级或双协议运行。省略 protocol 参数时使用官方协议；前后端自定义 schema 为 `oc-tldraw-2`，TLDraw 包必须同版本部署。

PostgreSQL/Django durable snapshot/event、document clock 和 reconciliation 状态是领域恢复事实源；SQLite 是同步协议 journal/cache。Redis room lease 协调房间归属，无 Redis 时仅支持单进程部署。lease 丢失会关闭房间并要求客户端重连。`/health` 报告房间、lease、pending persistence、clock/hash 和恢复状态。

历史 records-v1 快照可导入。冻结的 `legacy-record-migration.js` 只读取 PostgreSQL 中快照游标之后的旧操作；操作缺失或无法还原时拒绝打开房间，不覆盖已有数据。历史操作写入已禁用；历史 GET 与管理命令保留用于迁移和维护。

官方同步与 PostgreSQL checkpoint 是两个阶段，不能用“已连接”代替持久化成功。生产启用前需要验证断网重连、reader 拒写、重启收敛、备份恢复以及有效 TLDraw license。
