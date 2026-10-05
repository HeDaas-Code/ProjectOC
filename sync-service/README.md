# ProjectOC 自托管画布同步

服务端保留 `records-v1` 兼容协议，并提供官方 TLDraw `tldraw-sync-v2` 房间协议入口。v2 由 `TLSocketRoom` / `SQLiteSyncStorage` 管理完整画布记录、tombstone、clock、schema 和同步事务；客户端与服务端必须使用相同 `TLDRAW_SCHEMA_VERSION`。旧客户端可继续使用 records-v1，升级期间不会静默覆盖新 schema。

PostgreSQL/Django durable snapshot/event、document clock 和 reconciliation 状态仍是领域恢复事实源；SQLite 仅是同步协议 journal/cache，Redis 只是实时广播。room lease 保证同一 workspace/branch/canvas 在全局只有一个活跃房间实例；lease 丢失会关闭房间并要求客户端重连。`/health` 会报告协议、schema、房间数、lease、pending persistence、clock/hash 和广播健康状态。生产启用前应使用官方 TLDraw Sync 客户端/服务端包完成同版本部署，并通过 staging 验收套件验证长期离线、删除恢复、undo/redo 和重启收敛。
