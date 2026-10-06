# ProjectOC contributor guidance

## Development workflow

- Treat `main` as the stable baseline. Start each new task from the latest `origin/main` on a separate feature branch; use `codex/` for Codex-created branches.
- Commit related changes in focused steps. Push the feature branch and open a pull request targeting `main`; do not push development commits directly to `main` or merge without the maintainer's instruction.
- Keep local `.env` files, runtime databases, sync journals, world repositories, backups, and logs out of Git.
- Read `docs/development.md` for commands and `docs/architecture.md` for system boundaries. Update current documentation when behavior or configuration changes.
- Run checks appropriate to the change. Frontend changes need type checking/build and relevant tests; synchronization changes need sync-service tests and browser collaboration checks; backend changes need relevant pytest coverage and migration checks.

## Code Review Rules

Focus on actionable correctness, data loss, access control, and regression issues. Explain the concrete trigger and affected behavior, reference the relevant code, and distinguish verified findings from assumptions. Avoid style-only findings.

### Collaboration and persistence

- The only live canvas transport is official `tldraw-sync-v2`. Historical `records-v1` decoding is allowed solely for migration; never re-enable legacy live writes or silent protocol fallback.
- Check durable acknowledgement, reconnect ticket refresh, room/lease ownership, snapshot version checks, concurrent clients, and recovery after process restart or network loss.
- Protect pending edits and persistence retries. Shared persistence must not silently overwrite a newer snapshot or discard a room with unsaved changes.

### Authorization and private data

- Enforce owner/editor/reader permissions on backend mutations and sync connections, even if frontend controls are disabled.
- Never expose API keys, session cookies, invite tokens, or sync secrets in logs, responses, generated docs, or Git content repositories.
- AI proposals require explicit review before becoming formal entities or relations.

### Frontend interactions and deployment

- Application actions use styled dialogs rather than browser `alert`, `prompt`, or `confirm`. Check keyboard focus, Escape/cancel behavior, input validation, and duplicate submissions or partial-failure retries.
- Check frontend/sync-service TLDraw compatibility and matching backend/sync credentials. Keep example environment files complete without real credentials.
- Verify that migration/deployment instructions preserve existing world repositories and database volumes.
