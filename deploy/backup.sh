#!/usr/bin/env bash
# Create a self-contained ProjectOC backup directory.
# PostgreSQL is the runtime source of truth; world_repos is the human-readable
# Git export. Neo4j is intentionally not required because it is a rebuildable
# projection, but the manifest records that it must be rebuilt after restore.
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
BACKUP_ROOT="${BACKUP_ROOT:-$PROJECT_ROOT/backups}"
WORLD_REPOS_ROOT="${WORLD_REPOS_ROOT:-$PROJECT_ROOT/world_repos}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${1:-$BACKUP_ROOT/projectoc-$STAMP}"

fail() { echo "backup: $*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail "docker is required"
[[ -d "$WORLD_REPOS_ROOT" ]] || fail "world repos directory does not exist: $WORLD_REPOS_ROOT"
mkdir -p "$(dirname "$BACKUP_DIR")"
[[ ! -e "$BACKUP_DIR" ]] || fail "backup destination already exists: $BACKUP_DIR"
TMP_DIR="$BACKUP_DIR.tmp"
[[ ! -e "$TMP_DIR" ]] || fail "temporary backup destination already exists: $TMP_DIR"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

mkdir -p "$TMP_DIR"

# Fail before producing a partial backup when the source database is not ready.
"${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER:-projectoc}" -d "${POSTGRES_DB:-projectoc}" >/dev/null

# PostgreSQL dump includes canvas snapshots, operation logs, entities, members,
# proposals, timelines and all other Django runtime state.
"${COMPOSE[@]}" exec -T postgres sh -c \
  'pg_dump --format=custom --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  > "$TMP_DIR/postgres.dump"

# A portable fixture is useful for inspection and SQLite-based recovery drills.
# It is not the production restore source; postgres.dump is authoritative there.
"${COMPOSE[@]}" exec -T backend sh -c \
  'python manage.py dumpdata core accounts canvas ai_agent auth.user --natural-foreign --natural-primary --indent 2' \
  > "$TMP_DIR/django-fixture.json"

# Preserve each world repository, including its Git history, without following
# host-specific absolute paths.
tar -C "$WORLD_REPOS_ROOT" -czf "$TMP_DIR/world_repos.tar.gz" .

cat > "$TMP_DIR/manifest.json" <<MANIFEST
{
  "format": "projectoc-backup-v1",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "database": "postgresql",
  "database_dump": "postgres.dump",
  "portable_fixture": "django-fixture.json",
  "world_repos_archive": "world_repos.tar.gz",
  "canvas_state": "included in postgres.dump (snapshots, CanvasSyncEvent and durable clocks)",
  "checksum_file": "SHA256SUMS",
  "neo4j": {
    "included": false,
    "restore_strategy": "rebuild projection from PostgreSQL"
  },
  "restore_requires_confirmation": true
}
MANIFEST

(cd "$TMP_DIR" && sha256sum postgres.dump django-fixture.json world_repos.tar.gz > checksums.sha256 && cp checksums.sha256 SHA256SUMS)
mv "$TMP_DIR" "$BACKUP_DIR"
trap - EXIT

echo "Backup created: $BACKUP_DIR"
echo "Verify with: sha256sum -c $BACKUP_DIR/SHA256SUMS"
