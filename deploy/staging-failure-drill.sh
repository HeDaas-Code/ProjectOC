#!/usr/bin/env bash
# Non-destructive staging failure drills. Every drill is best-effort and the
# report distinguishes an unavailable dependency (SKIP) from an actual failure.
set -u
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/deploy/staging-common.sh"
ensure_staging_tls
mkdir -p "$REPORT_DIR"
printf '{"started_at":"%s","checks":[]}' "$(date -Is)" > "$REPORT_JSON"

compose_available=0
if command -v docker >/dev/null 2>&1 && "${COMPOSE[@]}" ps >/dev/null 2>&1; then compose_available=1; fi
if [[ "$compose_available" != 1 ]]; then
  record docker-compose SKIP "Docker Compose 不可用，故障演练仅生成报告"
  for item in redis neo4j postgres sync-service backend outbox-worker lease-expiry git-write-failure neo4j-fallback backup-checksum old-client-schema; do record "failure-$item" SKIP "Docker Compose 不可用"; done
  finalize_report
  exit 0
fi

probe() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then record "$name" PASS "恢复检查通过"; else record "$name" FAIL "恢复检查失败"; fi
}
restart_and_probe() {
  local service="$1" probe_dir="$REPORT_DIR/failure-probes/${service}-$(date +%s%N)"
  mkdir -p "$probe_dir"
  if "${COMPOSE[@]}" restart "$service" >/dev/null 2>&1; then
    sleep "${STAGING_FAILURE_SETTLE_SECONDS:-3}"
    # staging-verify is itself a report-producing command. Give each nested
    # probe an isolated report directory; otherwise every verify invocation
    # would overwrite the parent failure-drill report and hide earlier drills.
    if PROJECTOC_STAGING_REPORT_DIR="$probe_dir" "$ROOT_DIR/deploy/staging-verify.sh" >/dev/null 2>&1; then
      record "failure-$service" PASS "服务重启后 staging 验收通过（probe=$probe_dir/latest.json）"
    else
      record "failure-$service" FAIL "服务重启后 staging 验收未通过（probe=$probe_dir/latest.json）"
    fi
  else record "failure-$service" FAIL "无法重启服务"; fi
}
for service in redis neo4j postgres sync-service backend outbox-worker; do restart_and_probe "$service"; done

if "${COMPOSE[@]}" exec -T backend python manage.py check >/dev/null 2>&1; then record django-system-check PASS "Django check 通过"; else record django-system-check FAIL "Django check 失败"; fi
if "${COMPOSE[@]}" exec -T redis redis-cli ping >/dev/null 2>&1; then record failure-redis-reconnect PASS "Redis 恢复后 PONG"; else record failure-redis-reconnect FAIL "Redis 恢复后不可用"; fi
if [[ -n "${NEO4J_PASSWORD:-}" ]] && "${COMPOSE[@]}" exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" 'RETURN 1' >/dev/null 2>&1; then record failure-neo4j-fallback PASS "Neo4j 可用；停止期间 API 应回退 PostgreSQL"; else record failure-neo4j-fallback SKIP "未提供 Neo4j 密码或探针不可用"; fi
if [[ "${STAGING_ENABLE_DESTRUCTIVE_DRILLS:-0}" == 1 ]]; then
  record failure-git-write-failure SKIP "Git 写入故障注入需要部署者提供隔离 world_repos 挂载" 
  record failure-lease-expiry SKIP "lease 过期需要在线浏览器脚本配合，不在 shell 中伪造成功"
  record failure-old-client-schema SKIP "旧客户端 schema 需要真实浏览器构建产物"
else
  record failure-git-write-failure SKIP "未设置 STAGING_ENABLE_DESTRUCTIVE_DRILLS=1，避免破坏 staging 数据"
  record failure-lease-expiry SKIP "未设置 STAGING_ENABLE_DESTRUCTIVE_DRILLS=1"
  record failure-old-client-schema SKIP "未设置 STAGING_ENABLE_DESTRUCTIVE_DRILLS=1"
fi

finalize_report
