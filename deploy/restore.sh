#!/usr/bin/env bash
# Restore a ProjectOC backup. This is intentionally destructive for the
# PostgreSQL database and requires CONFIRM_RESTORE=YES.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${PROJECTOC_STAGING_COMPOSE:-0}" == "1" ]]; then
  COMPOSE=(
    docker compose
    -p "${COMPOSE_PROJECT_NAME:-projectoc-staging}"
    -f "$PROJECT_ROOT/docker-compose.yml"
    -f "$PROJECT_ROOT/deploy/docker-compose.prod.yml"
  )
else
  COMPOSE=(docker compose --project-directory "$PROJECT_ROOT")
fi
WORLD_REPOS_ROOT="${RESTORE_WORLD_REPOS_ROOT:-${WORLD_REPOS_ROOT:-$PROJECT_ROOT/world_repos}}"
TARGET_DB="${RESTORE_POSTGRES_DB:-${POSTGRES_DB:-projectoc}}"
BACKUP_DIR="${1:-}"

fail() { echo "restore: $*" >&2; exit 1; }
[[ -n "$BACKUP_DIR" ]] || fail "usage: CONFIRM_RESTORE=YES $0 <backup-directory>"
[[ "$CONFIRM_RESTORE" == "YES" ]] || fail "set CONFIRM_RESTORE=YES to permit destructive restore"
[[ -f "$BACKUP_DIR/manifest.json" ]] || fail "missing manifest.json in $BACKUP_DIR"
[[ -f "$BACKUP_DIR/postgres.dump" ]] || fail "missing postgres.dump in $BACKUP_DIR"
[[ -f "$BACKUP_DIR/world_repos.tar.gz" ]] || fail "missing world_repos.tar.gz in $BACKUP_DIR"
command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"

(cd "$BACKUP_DIR" && sha256sum -c "${CHECKSUM_FILE:-SHA256SUMS}")
"${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER:-projectoc}" -d "$TARGET_DB" >/dev/null

# Restore PostgreSQL first. If this fails, the world repository is untouched.
cat "$BACKUP_DIR/postgres.dump" | "${COMPOSE[@]}" exec -T postgres sh -c \
  'pg_restore --clean --if-exists --no-owner --username="$POSTGRES_USER" --dbname="$1"' sh "$TARGET_DB"

# Keep a rollback copy of the current content repositories before replacing
# them. The backup remains available as the primary rollback artifact.
if [[ -d "$WORLD_REPOS_ROOT" ]]; then
  OLD_ROOT="${WORLD_REPOS_ROOT}.pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$WORLD_REPOS_ROOT" "$OLD_ROOT"
  echo "Previous world repositories moved to: $OLD_ROOT"
fi
mkdir -p "$WORLD_REPOS_ROOT"
tar -xzf "$BACKUP_DIR/world_repos.tar.gz" -C "$WORLD_REPOS_ROOT"

if [[ "${RESTORE_SKIP_MIGRATE:-0}" != "1" ]]; then
  "${COMPOSE[@]}" run --rm backend python manage.py migrate --noinput >/dev/null
fi
cat <<MSG
Restore completed from: $BACKUP_DIR
PostgreSQL database: $TARGET_DB
World repositories: restored to $WORLD_REPOS_ROOT
Neo4j: not restored; rebuild the projection from PostgreSQL before enabling graph analysis.
MSG
