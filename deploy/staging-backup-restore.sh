#!/usr/bin/env bash
# Backup validation plus an isolated, opt-in restore rehearsal. The default
# path never destroys the running staging database or world repositories.
set -u
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/deploy/staging-common.sh"
mkdir -p "$REPORT_DIR"
BACKUP_DIR="${1:-$REPORT_DIR/backup-$(date +%Y%m%d-%H%M%S)}"
printf '{"started_at":"%s","checks":[]}' "$(date -Is)" > "$REPORT_JSON"

if "$ROOT_DIR/deploy/backup.sh" "$BACKUP_DIR" >/dev/null 2>&1; then record backup PASS "$BACKUP_DIR"; else record backup FAIL "备份创建失败"; finalize_report; exit 1; fi
if [[ -f "$BACKUP_DIR/manifest.json" && -f "$BACKUP_DIR/SHA256SUMS" && -f "$BACKUP_DIR/checksums.sha256" ]]; then record backup-manifest PASS "manifest 与双兼容 checksum 文件存在"; else record backup-manifest FAIL "manifest/checksum 缺失"; fi
if (cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then record backup-integrity PASS "SHA-256 校验通过"; else record backup-integrity FAIL "SHA-256 校验失败"; fi

# Explicitly test checksum failure without touching the real backup.
CORRUPT_DIR="$BACKUP_DIR.checksum-drill"
rm -rf "$CORRUPT_DIR"; cp -a "$BACKUP_DIR" "$CORRUPT_DIR"
printf '\ncorruption drill\n' >> "$CORRUPT_DIR/world_repos.tar.gz"
if (cd "$CORRUPT_DIR" && ! sha256sum -c SHA256SUMS >/dev/null 2>&1); then record backup-checksum-failure PASS "损坏备份被拒绝"; else record backup-checksum-failure FAIL "损坏备份未被拒绝"; fi
rm -rf "$CORRUPT_DIR"

if [[ "${STAGING_RESTORE_ISOLATED:-0}" == 1 ]]; then
  ISOLATED_DIR="${STAGING_RESTORE_DIR:-$REPORT_DIR/isolated-restore-$(date +%s)}"
  rm -rf "$ISOLATED_DIR"; mkdir -p "$ISOLATED_DIR/world_repos"
  if (cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS >/dev/null 2>&1) && tar -xzf "$BACKUP_DIR/world_repos.tar.gz" -C "$ISOLATED_DIR/world_repos"; then
    record isolated-world-repo-restore PASS "$ISOLATED_DIR/world_repos"
  else
    record isolated-world-repo-restore FAIL "隔离 world_repos 恢复失败"
  fi

  # Restore the custom dump into a disposable database inside the staging
  # PostgreSQL server.  This exercises the actual archive and PostgreSQL
  # server, without replacing the running staging database.
  POSTGRES_USER="${POSTGRES_USER:-projectoc}"
  POSTGRES_DB="${POSTGRES_DB:-projectoc}"
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-projectoc}"
  ISOLATED_DB="projectoc_restore_$(date +%s)_${RANDOM}"
  ISOLATED_DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${ISOLATED_DB}"
  isolated_db_created=0
  cleanup_isolated_db() {
    if [[ "$isolated_db_created" == 1 && "${STAGING_KEEP_ISOLATED_DB:-0}" != 1 ]]; then
      "${COMPOSE[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=0 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$ISOLATED_DB' AND pid <> pg_backend_pid();" \
        -c "DROP DATABASE IF EXISTS \"$ISOLATED_DB\";" >/dev/null 2>&1 || true
    fi
  }
  trap cleanup_isolated_db EXIT

  if "${COMPOSE[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      -c "CREATE DATABASE \"$ISOLATED_DB\";" >/dev/null 2>&1; then
    isolated_db_created=1
    if cat "$BACKUP_DIR/postgres.dump" | "${COMPOSE[@]}" exec -T postgres pg_restore \
        --exit-on-error --clean --if-exists --no-owner --no-privileges \
        --username="$POSTGRES_USER" --dbname="$ISOLATED_DB" >/dev/null 2>&1; then
      migration_count="$("${COMPOSE[@]}" exec -T postgres psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$ISOLATED_DB" \
        -c 'SELECT count(*) FROM django_migrations;' 2>/dev/null | tr -d '\r' | tail -n 1)"
      core_table_count="$("${COMPOSE[@]}" exec -T postgres psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$ISOLATED_DB" \
        -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('core_worldworkspace','core_entity','core_relation','canvas_stagingcanvas');" 2>/dev/null | tr -d '\r' | tail -n 1)"
      if [[ "$migration_count" =~ ^[0-9]+$ && "$migration_count" -gt 0 && "$core_table_count" =~ ^[0-9]+$ && "$core_table_count" -ge 3 ]]; then
        record isolated-postgres-restore PASS "临时数据库 $ISOLATED_DB 恢复成功；migrations=$migration_count core_tables=$core_table_count"
      else
        record isolated-postgres-restore FAIL "恢复后核心表检查失败；migrations=$migration_count core_tables=$core_table_count"
      fi

      # Use the restored database for Django's real settings/check path.  The
      # URL is passed only to the already-running backend process and is never
      # written to the report.
      if "${COMPOSE[@]}" exec -T -e "DATABASE_URL=$ISOLATED_DATABASE_URL" backend \
          python manage.py shell -c 'from django.db import connection; connection.ensure_connection(); print("isolated database connection ok")' >/dev/null 2>&1; then
        record isolated-api-check PASS "Django successfully connected to restored isolated database"
      else
        record isolated-api-check FAIL "Django 无法连接恢复后的隔离数据库"
      fi

      # Neo4j remains a rebuildable projection.  Running the projection
      # worker against the restored database proves the replay path without
      # pretending that Neo4j itself is a source-of-truth restore artifact.
      if "${COMPOSE[@]}" exec -T -e "DATABASE_URL=$ISOLATED_DATABASE_URL" backend \
          python manage.py process_graph_projection_jobs --limit "${STAGING_PROJECTION_LIMIT:-1000}" >/dev/null 2>&1; then
        record isolated-neo4j-rebuild PASS "从隔离 PostgreSQL 恢复的 projection jobs 已执行"
      else
        record isolated-neo4j-rebuild FAIL "隔离 PostgreSQL 上的 Neo4j projection 重建失败"
      fi
    else
      record isolated-postgres-restore FAIL "pg_restore 无法恢复到临时数据库"
      record isolated-api-check SKIP "隔离数据库恢复失败，未执行 Django 检查"
      record isolated-neo4j-rebuild SKIP "隔离数据库恢复失败，未执行 projection 重建"
    fi
  else
    record isolated-postgres-restore FAIL "无法创建临时 PostgreSQL 数据库"
    record isolated-api-check SKIP "临时数据库创建失败，未执行 Django 检查"
    record isolated-neo4j-rebuild SKIP "临时数据库创建失败，未执行 projection 重建"
  fi
else
  for item in isolated-world-repo-restore isolated-postgres-restore isolated-neo4j-rebuild isolated-api-check; do record "$item" SKIP "设置 STAGING_RESTORE_ISOLATED=1 才执行隔离演练"; done
fi

finalize_report
