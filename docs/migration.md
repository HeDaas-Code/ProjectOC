# records-v1 退役与升级

[返回文档索引](README.md)

## 当前行为

所有画布统一使用 `tldraw-sync-v2`。旧 `records-v1` WebSocket 请求返回 HTTP 410；内部 `/sync-ops/` 写入和带 `protocol=records-v1` 的 `/sync-state/` 写入同样返回 410。不存在旧协议回退路径。

旧 `TLDRAW_SYNC_OFFICIAL_ENABLED` 和 `VITE_TLDRAW_OFFICIAL_SYNC` 配置不再选择协议。历史数据库表、快照与操作日志保留；认证后的内部 `/sync-ops/` GET 只供历史迁移读取。

## 升级步骤

1. 停止编辑流量，检查同步服务 `/health`，确认待持久化队列清空；有错误时先检查原因。
2. 备份数据库、实际挂载的世界观内容目录与所需密钥，见 [备份说明](../deploy/README.md#备份与恢复)。
3. 同步更新前端、后端和同步服务，重新构建并启动。
4. 刷新浏览器旧页面，确认使用官方同步。
5. 检查旧本地备份提示，导出并人工核对未发送内容。
6. 验证画布内容、只读角色、断网重连和 Git 历史；确认迁移结果后再考虑历史日志维护。

不要通过启动旧协议服务回滚。如果需要回退版本，应恢复经过验证的数据库与内容备份，并使用一致的应用版本。

## 历史数据导入

`sync-service/src/legacy-record-migration.js` 是冻结的旧操作解码器，只还原 PostgreSQL 中快照 checkpoint 之后的历史日志，不接收新编辑。官方房间加载旧数据时先还原日志，再导入官方存储。

日志存在缺口、依赖无法还原或数据损坏时停止迁移并报错，避免用不完整快照覆盖现有数据。此时应保留原记录，检查备份与错误日志；不要删除日志后重新尝试。

## 浏览器本地数据

- `oc:sync-pending-ops:<canvas-id>`：旧版未发送操作，保留原样。
- `oc:legacy-pending-ops-backup:<canvas-id>`：此前保留的旧操作备份。
- `oc:draft:<canvas-id>`：旧版本地完整快照。
- `oc:official-draft:<canvas-id>`：官方同步路径的新本地快照备份。

画布提供旧操作和旧快照的导出入口。旧操作与官方协议的时钟、冲突规则不同，不能直接重放；导出不代表已经合并到共享画布。请人工核对需要保留的内容。

## 恢复边界

SQLite journal 与 PostgreSQL durable snapshot/event 会在房间打开时对账。可回放时补齐事件，无法对账时按 PostgreSQL 重建。实时连接状态不能替代持久化确认；数据库故障后强制终止进程等场景仍需独立验证。
