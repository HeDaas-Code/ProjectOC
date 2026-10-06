> 当前版本变更：records-v1 已退役。以下内容为历史实现记录，旧协议兼容、回滚和降级描述不再适用。当前运行方式见 [项目 README](../../README.md)。

# M6 implementation status

这是一个可运行的 M6 增量基础，不是完整的协作产品发布版。部署目标仍是可信的私有网络；生产启用协作前必须完成 HTTPS/WSS、备份恢复、许可和多浏览器验证。

## 已交付

### 身份与工作区访问

- Django session 登录、登出、当前用户和 CSRF 保护的变更接口。
- 事务串行化的一次性首个 owner bootstrap；首个 owner 创建后关闭公开注册。
- 邮箱绑定、过期、一次性邀请；后续用户必须通过邀请加入。
- workspace 的 `owner`、`editor`、`reader` 成员角色与 REST 访问过滤。
- reader 不能写画布、提案、对话和提交；成员与邀请管理仅 owner 可用。
- `DEBUG=0` 时的安全 session/CSRF cookie 和可配置 TLS 代理设置。

### 分支与合并

- `WorldBranch` 保存创建时的 `base_snapshot`、`base_commit` 和 Git ref；创建分支时将 main 基线物化为 branch-owned rows。
- 分支读取不再继承不断变化的 main：实体和关系使用 `base_entity` / `base_relation` 做 copy-on-write；分支新增、修改和归档都在分支行或 tombstone 上完成。
- main 删除使用归档状态，不物理删除，从而保留三方比较所需的逻辑身份。
- 未合并分支不会进入默认 main 图谱；`branch` 查询返回该分支自己的有效视图。
- 分支 Git history/diff、归档、工作树和 workspace 级 Git 锁已接入。
- merge preview 支持实体/关系三方比较、main 侧删除检测、重复标题、实体更新、关系更新和显式 resolution。
- 已增加字段级三方比较：非冲突字段自动合并，冲突字段返回 base/main/branch 值；确认接口支持逐字段选择 `keep_main`、`keep_branch` 或提交用户编辑后的 `{value: ...}`。
- 前端已提供分支合并审核面板，显示字段差异、冲突类型和最终值编辑，不再依赖浏览器 prompt。
- merge 使用 workspace 锁、preview token 和幂等状态；合并后保留分支审计副本并标记为 merged。
- 已实现 AI merge suggestion：只读取当前预览并返回经过 schema 校验的字段级建议；用户必须在前端显式应用建议后，再确认最终合并；上游失败时保留预览并允许人工处理。重复标题仍使用实体级 `keep_both` / `skip_branch_entity`。

### 持久化 outbox worker

- 新增 `run_outbox_worker` 管理命令和 Compose 独立 `outbox-worker` 服务。
- Git commit job 与 Neo4j projection job 使用 PostgreSQL lease，避免多个 worker 重复领取。
- 失败任务记录尝试次数、最后尝试时间、下次尝试时间和 lease 到期时间。
- 支持指数退避、最大尝试次数和安全重试；达到上限后保留失败状态，等待维护人员处理。
- 同步 API 与后台 worker 共用单 job 路径，避免手动触发和后台 worker 使用两套不同逻辑。
- 新增三项 worker 单元测试，覆盖 Git 失败重试、Neo4j 失败重试和 lease 隔离。

### Git 崩溃恢复与 reconciliation

- Git commit message 写入 `OC-Job: <job id>` marker。
- `GitRepositoryService.find_commit_by_marker()` 可从已有 commit 恢复数据库 job 的 commit hash，避免进程在 Git commit 后崩溃导致重复提交。
- 新增 `reconcile_commit_jobs` management command：
  - 恢复 `git_syncing`/未完成 job 的 marker commit；
  - 没找到 marker 时安全放回 `database_committed` outbox 状态；
  - 记录 branch ref 缺失并从已保存 `base_commit` 重建，不从 moving main 错误创建；
  - `--retry` 可在 reconciliation 后重试可恢复任务；新增 `/workspaces/{id}/git/status/` 提供 main/branch ref、基线和 orphan ref 的只读健康信息。
- 数据库确认、Git 同步、图谱投影仍通过可重试状态机协调，不假装实现跨系统分布式事务。

### AI 合并建议

- `POST /api/v1/branches/{id}/merge-suggestion/` 使用现有 OpenAI-compatible provider 分析当前三方冲突。
- 返回冲突 ID、整行/字段级选择、理由和置信度；服务端拒绝未知冲突、未知字段和非法选择。
- 建议是非绑定的临时结果，不会写入实体、关系或分支；前端必须先显式“应用建议”，再由用户确认合并。
- provider 不可用或返回非法 JSON 时，返回 503 和原 `preview_token`，不影响人工审核。

### Neo4j 图谱投影

- PostgreSQL 是唯一事实源，Neo4j 只是可选、可重建 projection。
- projection outbox 记录尝试次数、失败信息和成功状态；支持 owner-only health/rebuild。
- shortest path、impact analysis 和 branch projection 基础逻辑已提供；Neo4j 不可用时回退 PostgreSQL。
- `process_graph_projection_jobs` 可重试投影任务。
- 前端版本与维护页汇总 main/branch ref、孤立 ref、待处理任务和投影健康状态；支持 owner 请求投影重建。
- 图谱详情页已接入最短路径与影响范围分析，并显示 Neo4j/PostgreSQL 来源。
- 已完成一次真实 Compose Neo4j 联调烟测：后端容器加载官方 Python driver，branch 投影、最短路径、影响分析和重复 rebuild 均通过；仍未完成大规模图遍历压测、自动 worker/scheduler 和完整 temporal projection。
- 真实联调测试位于 `backend/tests/test_neo4j_integration.py`，默认跳过，使用 `RUN_NEO4J_INTEGRATION=1` 显式执行。

### 自托管实时协作

- 自托管 Node.js WebSocket sync service；每张画布对应隔离房间。
- 最近增量：完成 `records-v1` 操作级同步和真实 WebSocket 双 editor/reader 集成测试；验证并发字段合并、删除 tombstone、旧更新拒绝、重复操作幂等、reader 只读和 PostgreSQL 快照持久化。
- HMAC ticket 携带用户、workspace、canvas 和角色；reader 只读，editor/owner 可编辑。
- presence、cursor、selection、heartbeat、重连和在线成员状态已实现。
- 快照持久化使用 PostgreSQL 行锁、CAS 版本和 `CanvasRevision` 历史。
- 已提供 origin allow-list、payload/rate/capacity 限制、Nginx HTTPS/WSS 示例和生产 Compose 配置。
- **当前正常协作路径已切换为 `records-v1` record-level operations：`put`/`remove`、字段级确定性 LWW、删除 tombstone、操作幂等去重、快照物化。它仍不是 TLDraw 原生 operation-level CRDT，也不是完整离线 CRDT。**
- sync-service 已增加 Redis pub/sub 多实例房间传播；Redis 只负责实时广播，Django/PostgreSQL durable operation log 仍是恢复事实源，实例初始化时会通过 cursor replay 追赶错过的操作。`/health` 会报告实例 ID 和 pub/sub 健康状态。

### 时间线与人物基础

- 自定义时间轴、规范化可排序时间值、timeline/event、参与者、人物 lifespan 和有效期关系。
- 时间体系现在支持可选的确定性复杂日历规则：`variable_months` 月份长度、周期闰日、纪元标签和无零年显示；旧的固定 `units` 格式保持兼容，规则会进入 Git 时间体系索引。
- timeline/lifespan 支持分支 copy-on-write、归档和 merge；支持 as-of 人物/事件/关系图切片。
- Git snapshot 包含实体、关系、时间系统、时间体系换算、timeline entries 和 lifespan 索引；后端提供有向 affine 换算（可组合路径、精确分数结果）、结构化时间线 snapshot/diff API，以及带 `commit` 参数的历史时间切片图谱 API；前端已提供时间体系/换算编辑器、可视化时间轴、事件条带、人物生命周期条带、范围滑块、切片游标、版本选择和只读历史回放。

## 尚未交付 / 重要限制

- 已有单实例 outbox worker、lease、指数退避和最大尝试次数；仍需在生产环境做长期运行、告警和多实例压力验证。
- 真实大规模图遍历压测、projection 与 `CommitJob.PROJECTION_SYNC_FAILED` 的更细粒度联动。
- 离线编辑的客户端操作队列、去重、严格 schema 校验、容量保护、待发送数量提示和重连重放已补齐基础路径；多实例房间协调代码已接入 Redis pub/sub，并通过共享内存 transport 和真实 Redis 双实例集成测试验证；仍需完整 CRDT 语义。当前协议只保证在线房间内的确定性字段级 LWW，并对离线操作采用服务端确认/拒绝后的可重放策略。
- Playwright 主路径已验证账户、邀请、presence、reader REST/WebSocket 拒写、断网队列重放、快照恢复，以及编辑器实际创建的 `oc-card` 在另一浏览器中渲染；真实浏览器连接到重启后的 sync-service 并从 durable operation log 恢复的 E2E 已通过；真实 HTTPS/WSS 部署演练和生产 TLDraw license 验证仍未完成；backup/restore 的隔离恢复演练已通过，但仍需生产环境定期演练。
- 已新增 `deploy/backup.sh` 与 `deploy/restore.sh`：备份 PostgreSQL custom dump、Django portable fixture、`world_repos/` Git 历史并生成 manifest/SHA-256 校验；恢复前要求显式确认，并将旧内容仓库保留为回滚目录。已在当前 Compose PostgreSQL 上完成非破坏性备份、校验，以及使用临时数据库和临时世界观仓库的隔离恢复演练；恢复结果包含 Django 迁移记录和 Git 历史。Neo4j 重建演练仍待完成。
- 时间线 UI 已支持时间体系、生命周期、事件、参与者、时间体系换算、as-of 切片编辑与 Git 提交的只读回放；当前换算采用显式有向 affine 规则，复杂日历的自然语言解析、两个版本之间的完整可视化对比和浏览器 E2E 仍待补齐。复杂日历规则必须显式保存并校验，AI 不负责直接解释成事实。

## 生产前置条件

使用 HTTPS/WSS 反向代理；为 `SYNC_TICKET_SECRET` 和 `SYNC_INTERNAL_SECRET` 配置强随机且相互独立的值；仅暴露私有网络；配置数据库、Neo4j、Git 内容仓库和画布快照备份；设置 `DEBUG=0`、明确的 allowed hosts 和 secure cookies；验证有效 TLDraw 生产许可。禁止使用公开演示同步服务器。

## 验证记录（2026-10-04）

```text
cd backend
../.venv/bin/pytest -q                         # 49 passed, 1 skipped
../.venv/bin/python manage.py check             # no issues
../.venv/bin/python manage.py makemigrations --check --dry-run

前端：`npm test -- --run`（17 passed），`npm run build`（通过，保留第三方包与 bundle size warning）；sync-service：默认 `npm test`（17 passed，1 skipped；当前测试文件合计 18 个，其中 1 个跳过），新增 snapshot 409 冲突和进程重启后 durable operation replay 回归测试；Playwright `npm run test:e2e -- --workers=1`（1 passed），覆盖三浏览器权限、presence、reader 拒写、断网操作队列重放、实时卡片跨浏览器渲染、快照恢复以及真实 sync-service 重启后的浏览器恢复。`docker compose config -q` 通过；真实 Neo4j 联调已通过显式集成测试，但生产备份恢复演练和生产 TLS 验证仍未完成。
```

## 最新增量（2026-10-04）

- Playwright 隔离环境已补充 `CSRF_TRUSTED_ORIGINS`、Django/Sync 内部密钥和 `/sync-ops/` 内部认证权限，已通过首个 owner、邀请 editor/reader、三浏览器登录、presence、reader 只读、reader REST/WebSocket 拒写、浏览器断网操作队列重放、实时操作持久化以及 owner 断线恢复后的画布重新加载主路径。
- 画布前端增加浏览器 `offline`/`online` 事件处理，会主动关闭协作连接并进入自动重连状态。
- 为修复空 TLDraw 文档基线恢复问题，sync service 增加 baseline 握手/确认协议；baseline 只由 editor/owner 初始化，之后再接收 `records-v1` 增量操作。
- 最新验证：backend `49 passed, 1 skipped`；frontend unit `17 passed`；frontend build 通过；sync-service `17 passed, 1 skipped`（18 个测试，其中 1 个跳过；含 baseline 初始化、恢复、reader 拒绝、snapshot 409 冲突、Redis 多实例协调和重启后 durable operation replay 回归测试）；Playwright 主协作场景 `1 passed`，已通过 reader 实际看到 editor 创建的 `oc-card` 内容。
- 前端 `npm test` 已限制在 `src` 测试目录，不再把 `e2e/*.spec.ts` 误交给 Vitest；Playwright 仍通过独立的 `npm run test:e2e` 执行。
- 仍不能据此宣称完整生产协作完成：完整 CRDT/等价冲突合并语义、HTTPS/WSS 真实部署、TLDraw 生产 license、压力和长期运行验证仍待补齐；真实浏览器重启恢复 E2E 已通过；REST SaveQueue 与 realtime durable operation 的版本冲突已修复并增加回归测试；backup/restore 的隔离恢复演练已通过，但仍需生产环境定期演练。

## 最新验证补充

- 修复本地 SQLite 多浏览器并发写入的偶发 `database is locked`：SQLite WAL 初始化改为进程内串行且只执行一次，并增加仅作用于 SQLite 开发/E2E 回退的 mutating-request 写锁；生产 PostgreSQL 不依赖该保护。
- 在最新客户端离线重基逻辑之后重新执行 Playwright 主协作场景：`1 passed`；日志不再出现 `database is locked`，预期的旧版本 `409` 冲突仍正常返回。
- 回归后端测试：`49 passed, 1 skipped`。
- 该修复只解决本地 SQLite 并发噪声，不改变协作协议边界；`records-v1` 仍是字段级确定性 LWW + 服务端确认后的离线重放，不宣称完整 CRDT。
- 服务端 `ops_ack` 现在返回被 LWW 覆盖的字段、当前远端值和字段版本；前端已增加冲突审核面板，可逐条查看本地/远端值，并选择保留远端或以新 Lamport 时钟重试本地字段。
- 本次改动已通过前端 TypeScript/Vite 构建（仅保留既有第三方包与 bundle size warning）、前端单测 `19 passed`、sync-service 全测 `19 passed, 1 skipped`；后端回归仍为 `49 passed, 1 skipped`。冲突面板的真实双浏览器交互 E2E 尚待补充。
- 冲突解析逻辑已从 `CanvasSurface.vue` 抽为 `frontend/src/collaboration/conflicts.ts`，覆盖服务端 payload 归一化、本地/远端值读取、删除冲突和生成新 Lamport 时钟重试操作；对应前端单测新增 4 项，当前前端单测总计 `23 passed`。
- 新增冲突审核 CSS，面板可滚动且在窄屏下收缩；主 Playwright 协作场景重新执行 `1 passed`，未观察到 `database is locked`。

## 最新增量（2026-10-05）

- 新增 `compact_canvas_operations` Django maintenance command，支持按画布/工作区筛选、`--keep-last`、`--min-operations`、归档画布选择和 `--dry-run`。
- 命令与同步服务使用相同的安全边界：只会删除已经进入 PostgreSQL 持久化 snapshot checkpoint 的 operation，保留最近操作，并在行锁下幂等更新 `operation_compacted_through`；不会把尚未进入 snapshot 的 operation 当作可回收数据。
- 新增命令级测试：安全 checkpoint、保留窗口和 dry-run 不变更数据。
- 最新回归：backend `53 passed, 1 skipped`，frontend `23 passed`，frontend build 通过，sync-service `20 passed, 1 skipped`；Django check 与迁移检查通过。构建中的第三方包 directive 与 bundle size 仍是 warning，不是本次失败。

## 最新增量（2026-10-05，因果元数据）

- `records-v1` 操作现在支持可选 `deps` causal vector；服务端持久化并回传 `state_vector`，缺少前置操作时返回可重试的 `deferred/missing_dependencies`，不会把它误记为永久拒绝。
- 旧快照没有状态向量时，会从已有字段版本与删除 tombstone 推导保守向量；重复操作、快照恢复和批量按序依赖均有回归测试。
- 修复 baseline ack 回归断言，并新增因果依赖、状态向量恢复和非法向量校验测试。
- 最新验证：backend `53 passed, 1 skipped`；frontend `23 passed`，TypeScript/Vite build 通过；sync-service `23 passed, 1 skipped`；Django check 与 migration check 通过。
- 该增量仍然只是因果元数据增强，不改变边界：当前协议不是完整 CRDT，仍缺 TLDraw shape 专用合并、文本级 CRDT、完整离线多端 merge 和生产压力验证。


## 最新增量（2026-10-05，一致性报告）

- 新增只读一致性服务 `backend/apps/core/services/consistency.py`，沿用 PostgreSQL 有效分支视图，不会把 AI 建议或未合并分支写入正式数据。
- 新增 `GET /api/v1/workspaces/{id}/consistency/?branch=main`，支持 `main` 与活动工作分支；报告包含 0–100 分数、错误/警告/信息摘要、实体/关系数量和稳定排序的问题列表。
- 初版确定性检查覆盖：重复标题、空正文、孤立实体、未链接 floating tip、悬空关系、人物缺生命周期、时间线缺事件、事件缺参与者、非法时间区间和冲突关系统计。
- 维护面板已展示一致性分数和前三项问题，并随工作分支切换刷新；AI 仍只能读取报告，不能自动修改正式实体。
- 回归验证：backend `55 passed, 1 skipped`；frontend `23 passed`；前端 TypeScript/Vite build 通过（保留既有第三方 directive 与 bundle size warning）。

这使“设计完成度”前进了一小步，但不改变总体边界：完整 CRDT、生产 TLS/WSS、TLDraw 商业许可、压力/长期运行演练、AI 全局维护与大图谱性能仍然是从“可运行预发布”到“生产级完整产品”之间的主要工作。

## 最新增量（2026-10-05，前端因果重基）

- `frontend/src/collaboration/offlineOps.ts` 的离线乐观重基现在与 sync-service 使用同一套因果优先排序：因果上较新的本地操作即使 Lamport clock 较小，也不会被已观察到的旧远端字段覆盖；并发操作继续使用稳定的版本元组排序。
- 新增浏览器侧回归测试，覆盖“因果后续胜过较大 Lamport 基线”和“并发编辑保持确定性”两条路径。
- 最新验证：frontend `25 passed`，TypeScript/Vite build 通过；保留既有第三方 `use client` directive 与 bundle size warning。
- 该修复只统一前端乐观显示与服务端仲裁语义，仍不等同于完整 CRDT；shape 专用合并、文本级 CRDT 和长期离线多端合并仍待实现。

## 最新增量（2026-10-05，前端 AI 维护审阅）

- `MaintenanceStatusPanel` 已接入 `POST /api/v1/workspaces/{id}/maintenance-review/`，支持当前 `main` 或活动工作分支。
- 维护面板现在可以手动触发 Agent 审阅，并区分上游 Agent 与离线规则 fallback；请求中、上游不可用和普通错误均有独立提示。
- 建议以 `ask`、`inspect`、`draft_proposal` 标签展示，显示建议理由、问题优先级和可跳过的下一步问题。
- 一致性报告与维护建议中的实体/关系目标可以点击定位到图谱；关系目标会解析其 source 实体后打开图谱详情。
- 面板明确标注：维护建议不会自动创建提案、修改实体、接受关系或提交正式世界观；reader 只能查看建议，没有任何接受/提交入口。
- 新增维护建议标签和模式显示的前端回归测试；最新验证：frontend `26 passed`，TypeScript/Vite build 通过。

该增量完成了维护 Agent 的第一版前端闭环，但仍不是全局自动维护：后续仍需跨时间线的更深一致性分析、长期世界观记忆、从建议显式创建审核提案，以及生产级多模型/限流/审计能力。

## 最新增量（2026-10-05，TLDraw shape 合并语义）

- `sync-service/src/record-sync.js` 现在为 TLDraw shape 明确区分 geometry、content、style、hierarchy 和 metadata 字段类别。
- `x/y/w/h/rotation`、`text/richText/props`、`color/fill/size/font/align` 继续按字段执行因果优先 LWW，因此同一 shape 的不同字段可以安全合并。
- `parentId/index` 被视为同一个 hierarchy group；组内并发编辑采用因果优先、并发时使用稳定版本元组裁决，避免层级和顺序被不同操作拆成不一致组合。
- hierarchy group 版本与因果上下文会进入快照元数据并在恢复后继续生效。
- 冲突返回 `field_categories` 与 `merge_strategies`，前端可以向用户解释这是内容、样式、几何还是层级冲突。
- 删除仍使用 tombstone；后续因果上更新的操作可以恢复，旧操作不会复活已删除 shape。当前仍不宣称完整 CRDT，也没有引入文本级 CRDT。
- 最新 sync-service 回归：`26 passed, 1 skipped`。

## 最新增量（2026-10-05，维护提案前端闭环）

- `HistoryPanel` 现在把当前画布、工作区编辑权限和维护面板连接起来；维护 Agent 的 `draft_proposal` 建议可在 editor/owner 视角直接填写标题、候选内容和实体类型。
- 提案创建成功后由工作台刷新当前画布的正式提案列表，因此提案审核计数和画布草稿状态不会停留在旧数据；仍然遵守“先提案、后人工确认、再提交正式世界观”。
- `App.vue` 从当前 workspace 成员列表解析当前用户角色；角色解析失败时默认只读，避免前端状态异常授予写权限。reader 不显示创建待审核提案的入口，后端 403 仍是最终权限边界。
- 验证：frontend `28 passed`，TypeScript/Vite production build 通过；保留既有第三方 `use client` directive 和 bundle size warning。

## 最新增量（2026-10-05，协作冲突审核可操作性）

- 修复 TLDraw 菜单层级遮挡协作冲突审核面板的问题：冲突面板 `z-index` 提升到高于 TLDraw 的 UI 菜单层级，颜色/样式选择器打开时仍可点击“保留远端”和“重试本地值”。
- 原因是冲突面板此前使用 `z-index: 20`，而 TLDraw 菜单使用 300 以上的独立 UI 层；面板虽然可见，但按钮可能无法通过浏览器命中测试。
- 针对真实断网冲突场景重新运行 Playwright：冲突出现、重试本地值、操作发送和本地内容恢复均通过。
- 最新前端验证：Vitest `28 passed`；生产 TypeScript/Vite build 通过；Playwright 全量协作场景 `2 passed`。

## 最新增量（2026-10-05，Agent 分支感知上下文）

- 修复 `DialogueOrchestrator.context()` 原先按整个 workspace 读取实体的问题。
- 产婆式对话现在严格使用当前画布绑定的 `WorldBranch` 有效视图，包含当前分支的实体覆盖、分支新增实体和有效关系；不会泄漏分支创建后新增的 `main` 实体或其他工作分支内容。
- AI 上下文现在显式携带当前 branch 的 ID、名称和状态，并将关系端点解析为当前分支实体标题，便于模型在正确的分支语义下提出建议。
- 新增分支隔离回归测试，覆盖 main 基线覆盖、分支新增实体、分支后 main 新增实体、其他分支实体和关系上下文。
- 验证：后端完整测试 `61 passed, 1 skipped`；该修复未改变 AI 提案必须先审核的约束。

## 最新增量（2026-10-05，TLDraw 嵌套字段合并）

- `sync-service` 的 `records-v1` 新增受校验的 `patches` 操作，可对 `props` 等嵌套字段按路径执行因果优先 LWW；不同子路径可以并发合并，祖先/后代路径会被识别为同一冲突域。
- 服务端支持嵌套字段的设置、删除、JSON 快照物化、版本元数据持久化、重启恢复和冲突详情；限制原型污染键、重叠 patch、非法 `value`/`unset` 组合和过大路径。
- `CanvasSurface` 在 TLDraw props 对象变化时生成嵌套 patch，并在离线恢复、远端操作、乐观重基和冲突重试中应用相同路径语义。
- 新增 sync-service 与 frontend 回归测试，覆盖并发 props 合并、嵌套删除、快照恢复、整体替换冲突、离线重基、冲突重试与恶意路径拒绝。
- 最新验证：sync-service `30 passed, 1 skipped`；frontend `31 passed`；TypeScript/Vite production build 通过。

这仍然不是文本级 CRDT，也没有解决长期离线多端合并；下一步需要处理富文本增量操作、离线多端因果合并和压力/恢复测试。

## 最新增量（2026-10-05，Agent 长期记忆第一版）

- 新增 `DialogueMemory`，按 `workspace + branch` 建立唯一、持久化的 Agent 记忆范围；main 与各工作分支不会共享记忆。
- 记忆严格分为 `confirmed_facts`、`working_notes`、`open_questions` 和最近会话摘要：正式事实只从当前分支的有效 `Entity/Relation` 重建，待审核提案不会自动晋升为事实。
- 记忆更新使用有界、确定性的压缩策略，限制事实、草稿、问题和消息摘要的数量与文本长度，避免跨会话上下文无限增长。
- 对话上下文现在携带记忆版本、正式事实、工作笔记和未解决问题；读取上下文时会重新从 PostgreSQL 有效分支视图刷新，其他会话完成提交后无需再对话即可看到正式事实。
- 新增 `GET /api/v1/dialogue/sessions/{id}/memory/`，供审核面板查看当前记忆范围和信任分层。
- 新增跨会话回归测试：验证提案阶段不进入 `confirmed_facts`，正式提交后在另一会话中可见，且分支 scope key 正确隔离。

本增量仍是长期记忆的确定性基础层；后续仍需增加语义摘要、用户可审计的记忆编辑、记忆归档/删除策略、跨实体/人物/时间线推理、成本控制和 Agent 操作审计。
- 前端 `DialoguePanel` 与 Pinia 工作台现在显示当前记忆 revision、正式实体/待审核草稿数量和记忆边界说明；切换画布、发送消息和重新打开会话时刷新记忆。
- 本增量最终验证：后端 `62 passed, 1 skipped`，前端 Vitest `31 passed`，前端 TypeScript/Vite production build 通过，Django system check 与 migration check 均通过。

## 最新增量（2026-10-05，Agent 记忆可审计编辑）

- `DialogueMemory` 增加用户维护的 `manual_notes` 与 `archived_questions`，与正式事实、待审核提案和自动摘要分离。
- 新增 `DialogueMemoryAudit` 追加式审计模型；用户对记忆笔记和问题归档/恢复的修改记录操作者、前后值和时间。
- `GET /api/v1/dialogue/sessions/{id}/memory/?audit=1` 返回最近 30 条记忆变更；`PATCH` 支持 `expected_revision` 乐观并发校验、编辑笔记、归档和恢复非阻塞问题。
- 记忆刷新改为幂等：读取不会因为当前问题的动态时间戳而产生虚假 revision；正式实体仍只从当前有效分支重建，用户笔记不会自动进入正式世界观。
- DialoguePanel 已提供笔记、问题归档/恢复和审计记录入口。
- 新增后端回归测试覆盖并发版本冲突、记忆编辑、问题归档和审计；后端当前 `63 passed, 1 skipped`。

这补齐了长期记忆的用户审计边界，但仍不是语义摘要/自动因果推理系统；复杂推理、成本控制和 Agent 操作级审计仍需后续迭代。

## 最新验证补充（2026-10-05）

- Django：`63 passed, 1 skipped`；`makemigrations --check`、迁移和 system check 通过。
- sync-service：`30 passed, 1 skipped`；覆盖 durable operation log、重放、重启恢复、多实例协调和嵌套 patch 单元语义。
- Frontend：Vitest `31 passed`，TypeScript/Vite production build 通过；记忆 409 冲突会先刷新服务端版本再提示用户重试。
- Playwright：协作 E2E `2 passed`，覆盖邀请角色、presence、reader 只读、断网队列、真实 sync-service 重启恢复和协作冲突审核。

## 最新收拢增量（2026-10-05，CRDT 与 staging 恢复验证）

- staging 备份/恢复脚本现在显式继承 `projectoc-staging` Compose 项目和生产 overlay，不再误备份开发栈数据库。
- `STAGING_RESTORE_ISOLATED=1 ./deploy/staging-backup-restore.sh` 已实现真实隔离 PostgreSQL 恢复：创建临时数据库、恢复 custom dump、校验 Django migration 与核心表、用恢复数据库运行 Django 连接检查，并执行 Neo4j projection job replay；临时数据库默认自动删除，可用 `STAGING_KEEP_ISOLATED_DB=1` 保留。
- 本轮真实 staging 结果：备份、manifest、SHA-256、损坏备份拒绝、world_repos 恢复、隔离 PostgreSQL 恢复、Django 检查、Neo4j projection replay 均 `PASS`。
- 官方 TLDraw `tldraw-sync-v2` 房间、schema 协商、SQLite protocol journal、PostgreSQL snapshot/event durable replay、room lease 和 legacy fallback 已在代码与单元测试中接通；`records-v1` 仍只用于旧画布/回滚。
- 全量验证：Django `67 passed, 1 skipped`；sync-service `34 passed, 1 skipped`；frontend `31 passed`；生产构建通过；staging verify `27 PASS, 0 FAIL, 2 SKIP`；failure drill `31 PASS, 0 FAIL, 5 SKIP`。
- `production_collaboration_enabled=false` 保持不变：当前未注入真实 `VITE_TLDRAW_LICENSE_KEY`，且 credentialed staging Playwright 门禁未配置；不能据此宣称生产协作已启用。

## 最新收拢增量（2026-10-05，官方 CRDT 持久化与迁移重试）

- `sync-service/src/tldraw-room.js` 的官方 `tldraw-sync-v2` 房间现在把每次已提交画布变更排入完整 snapshot 持久化队列；PostgreSQL 暂时不可用时保留 pending snapshot，使用指数退避（上限 30 秒）重试，并在 `/health` 暴露 pending、retry、in-flight、clock/hash 与 reconciliation 状态。
- `records-v1` legacy snapshot 首次迁移也走同一条持久化重试路径。迁移失败不会阻塞房间打开或丢失旧画布内容；成功 checkpoint 后清除 `migratedFromLegacy`，旧 snapshot 仍可用于回滚。
- 新增回归测试覆盖 legacy migration 首次 PostgreSQL 失败、房间继续服务、后续重试成功以及 protocol/migration metadata 保留。
- Timeline Diff 已修复 participant 数组稳定排序和 lifecycle 时间变化误报 `moved` 的问题；事件时间变化仍标记为 `moved`，生命周期字段变化标记为 `modified`，结果保持确定性排序。
- 最新验证：Django `69 passed, 1 skipped`；sync-service `36 passed, 1 skipped`；frontend `31 passed`；TypeScript/Vite production build 通过；所有 `deploy/*.sh` 通过 `bash -n`。
- 生产门禁仍保持 `production_collaboration_enabled=false`、`license_configured=false`：当前没有真实 `VITE_TLDRAW_LICENSE_KEY`、真实生产证书或 credentialed staging Playwright 凭据；本地自签名 HTTPS 和自动化单元/集成测试不等于官方许可或生产多浏览器验收。
- `deploy/staging-failure-drill.sh` 现在为每次服务重启验收使用隔离 probe report，避免内部调用 `staging-verify.sh` 覆盖父级故障演练报告；故障演练结果可完整保留 redis、neo4j、postgres、sync-service、backend、outbox-worker 六项重启记录。
- 最新自托管 staging 验收：`staging-up` 4 PASS/1 SKIP；`staging-verify` 27 PASS/0 FAIL/2 SKIP；隔离备份恢复 8 PASS/0 FAIL/0 SKIP；failure drill 9 PASS/0 FAIL/3 SKIP。所有服务随后已执行 `staging-down.sh` 清理。


## 当前版本：records-v1 退役

- 前端画布统一使用官方 useSync，移除旧 WebSocket 操作队列、冲突 UI、presence 广播与 REST 快照回退；旧本地操作和快照保持可导出。
- 服务端移除旧实时房间与 Redis pub/sub；旧协议连接和旧操作写入返回 410。保留历史日志 GET、数据库记录及冻结解码器用于一次性迁移。
- 迁移还原快照 checkpoint 后的历史日志，缺失日志时停止迁移；官方房间初始化和租约获取去重。断开最后一个客户端时，有待持久化任务的房间继续重试。
- 官方重连动态获取新票据；连接状态显示真实 online/offline。离线时阻止切换画布，避免卸载仍有本地编辑的同步客户端。
- 验证：后端 pytest 71 passed / 1 skipped（真实 Neo4j 联调需单独环境）；同步服务 35 passed；前端 12 passed（旧协议测试随模块移除）；TypeScript/Vite build、Django check、迁移检查及部署脚本语法检查通过。Playwright 官方协议三浏览器、reader 权限、断网编辑重连及同步服务重启恢复 1 passed。
- 上述验收未包含 PostgreSQL 故障后进程强制终止等全部持久化边界；生产许可和真实部署门禁保持独立。
