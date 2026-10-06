# 参与开发

感谢帮助改进 ProjectOC。开始前请阅读 [开发指南](docs/development.md) 和 [架构说明](docs/architecture.md)。

## 问题反馈

请在 Issue 中说明操作步骤、预期结果、实际结果、运行方式（Compose 或本地）、应用提交版本，以及相关错误日志。同步问题请补充涉及的成员角色、断网或重启情况，以及同步服务 `/health` 中的持久化状态。

提交前删去模型 API 密钥、Cookie、同步票据、邀请链接和私人世界观内容。不要提交 `.env`、数据库、备份或本地日志。

## 分支与 PR

`main` 是稳定基线。新任务从最新 `origin/main` 创建独立功能分支，Codex 创建的分支使用 `codex/` 前缀；开发提交推送到功能分支，通过 PR 审查后再合并到 `main`。

```bash
git fetch origin
git switch -c codex/your-feature origin/main
# 完成修改与验证后，分步提交
git push -u origin HEAD
gh pr create --base main
```

已有未合并改动时继续使用其分支；另一个独立任务从基线创建新分支或 worktree。不要覆盖其他任务的未提交文件。

Codex 审查遵循 [AGENTS.md](AGENTS.md)。在 [Codex 审查设置](https://app.chatgpt.com/settings/code-review) 中为仓库启用自动审查，或在 PR 评论中发送 `@codex review` 触发一次审查。审查反馈修复并验证后，由维护者决定合并。添加仓库规则文件本身不会开启云端自动审查。

## 代码与文档修改

- 一个改动聚焦一个问题，提交说明写清触发条件和最终行为。
- 根据改动运行相关后端、前端或同步服务测试；画布协作改动同时验证浏览器行为。
- 数据模型变更附带 Django migration，并运行迁移检查。
- 前端和同步服务的 TLDraw 依赖保持兼容，更新依赖时提交对应 lockfile。
- 功能、配置或启动方式变化时更新当前指南；历史规划留在 `docs/archive/`。
- 按模块或目的拆分提交，在 PR 中写明验证结果与仍未验证的部分。

正式数据需要经过审核流程；不要让 AI 建议自动写入正式实体，也不要恢复已退役的 `records-v1` 实时路径。

项目源码使用 [MIT License](LICENSE)。
