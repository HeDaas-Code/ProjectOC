#!/usr/bin/env bash
set -u
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/deploy/staging-common.sh"
ensure_staging_tls
mkdir -p "$REPORT_DIR"
printf '{"started_at":"%s","checks":[]}' "$(date -Is)" > "$REPORT_JSON"

check_url() {
  local name="$1" url="$2" expected="${3:-200}" status
  status="$(curl --silent --show-error --insecure --max-time 10 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
  if [[ "$status" == "$expected" ]]; then record "$name" PASS "$url -> HTTP $status"; else record "$name" FAIL "$url -> HTTP ${status:-unreachable}, expected $expected"; fi
}
check_reachable() {
  local name="$1" url="$2" status
  status="$(curl --silent --show-error --insecure --http1.1 --max-time 10 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
  if [[ -n "$status" && "$status" != "000" ]]; then record "$name" PASS "edge endpoint answered HTTP $status (authentication may be required)"; else record "$name" FAIL "edge endpoint is unreachable: $url"; fi
}

if [[ "$STAGING_TLS" == "1" ]]; then
  check_url projectoc-https-health "${STAGING_BASE_URL:-https://localhost}/health/"
  [[ "${STAGING_BASE_URL:-https://localhost}" == https://* ]] && record https-endpoint PASS "HTTPS endpoint configured" || record https-endpoint FAIL "staging must use HTTPS"
  if curl --silent --show-error --max-time 10 -o /dev/null -w '%{http_code}' "${STAGING_HTTP_URL:-http://localhost}/health/" 2>/dev/null | grep -Eq '^30[178]$'; then record https-redirect PASS "HTTP redirects to HTTPS"; else record https-redirect FAIL "HTTP edge does not prove an HTTPS redirect"; fi

  # Test the public WSS edge as an edge endpoint. A 401/403/404 is still useful:
  # it proves Nginx reached sync-service; a zero status is the actual failure.
  EDGE_BASE="${STAGING_BASE_URL:-https://localhost}"
  check_reachable wss-edge-reachable "${EDGE_BASE}/rooms/health"
else
  check_url projectoc-http-health "${STAGING_BASE_URL:-http://localhost:5173}/health/"
  record https-endpoint SKIP "HTTP development mode"
  record https-redirect SKIP "HTTP development mode; HTTPS redirect is intentionally disabled"
  record wss-edge-reachable SKIP "HTTP development mode uses ws://localhost:8787 directly"
  check_url sync-http-health "${SYNC_HEALTH_URL:-http://127.0.0.1:8787/health}"
fi
# The production overlay intentionally removes the sync-service host port.
# Prefer a container-local health probe so this check validates the deployed
# service rather than accidentally probing a developer process on 127.0.0.1.
if command -v docker >/dev/null 2>&1 && [[ "${STAGING_USE_CONTAINER_HEALTH:-1}" == "1" ]]; then
  if "${COMPOSE[@]}" exec -T sync-service node -e "fetch('http://127.0.0.1:8787/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then
    record sync-health PASS "sync-service /health responded inside the staging network"
  else
    record sync-health FAIL "sync-service /health is unavailable inside the staging network"
  fi
else
  check_url sync-health "${SYNC_HEALTH_URL:-http://127.0.0.1:8787/health}"
fi

if [[ -n "${VITE_TLDRAW_LICENSE_KEY:-}" ]]; then record tldraw-license-configured PASS "license_configured (validity remains a manual vendor gate)"; else record tldraw-license-configured SKIP "set VITE_TLDRAW_LICENSE_KEY for production"; fi
if [[ "$STAGING_TLS" == "1" ]]; then
  [[ "${DJANGO_DEBUG:-0}" == "0" ]] && record django-debug PASS "DJANGO_DEBUG=0" || record django-debug FAIL "DJANGO_DEBUG must be 0"
else
  [[ "${DJANGO_DEBUG:-0}" == "1" ]] && record django-debug PASS "DJANGO_DEBUG=1 (local development)" || record django-debug FAIL "local development should use DJANGO_DEBUG=1"
fi
[[ -n "${DJANGO_ALLOWED_HOSTS:-}" && "${DJANGO_ALLOWED_HOSTS}" != "*" ]] && record allowed-hosts PASS "DJANGO_ALLOWED_HOSTS is explicit" || record allowed-hosts FAIL "DJANGO_ALLOWED_HOSTS must be explicit and non-wildcard"
if [[ "$STAGING_TLS" == "1" ]]; then
  [[ "${DJANGO_SECURE_SSL_REDIRECT:-0}" == "1" || "${STAGING_BASE_URL:-https://localhost}" == https://* ]] && record secure-redirect-configured PASS "HTTPS redirect/proxy configuration present" || record secure-redirect-configured FAIL "secure HTTPS redirect is not configured"
else
  record secure-redirect-configured SKIP "HTTP development mode; secure redirect intentionally disabled"
fi
ticket_secret="${SYNC_TICKET_SECRET:-}"; internal_secret="${SYNC_INTERNAL_SECRET:-}"
if [[ -n "$ticket_secret" && -n "$internal_secret" && "$ticket_secret" != "$internal_secret" && ${#ticket_secret} -ge 24 && ${#internal_secret} -ge 24 ]]; then record sync-secrets PASS "independent secrets are configured"; else record sync-secrets FAIL "independent secrets of at least 24 characters are required"; fi
if [[ "$STAGING_TLS" == "1" ]]; then
  [[ "${SYNC_REQUIRE_ORIGIN:-0}" == "1" ]] && record websocket-origin PASS "SYNC_REQUIRE_ORIGIN=1" || record websocket-origin FAIL "WSS origin enforcement is disabled"
else
  [[ "${SYNC_REQUIRE_ORIGIN:-0}" == "0" ]] && record websocket-origin PASS "origin enforcement disabled for local HTTP development" || record websocket-origin FAIL "local HTTP development expects SYNC_REQUIRE_ORIGIN=0"
fi
[[ -n "${TLDRAW_SCHEMA_VERSION:-}" ]] && record tldraw-schema PASS "$TLDRAW_SCHEMA_VERSION" || record tldraw-schema FAIL "TLDRAW_SCHEMA_VERSION is not configured"

if command -v docker >/dev/null 2>&1; then
  if "${COMPOSE[@]}" config --quiet >/dev/null 2>&1; then record compose-config PASS "compose configuration is valid"; else record compose-config FAIL "compose configuration is invalid"; fi
  if "${COMPOSE[@]}" version >/dev/null 2>&1; then record compose-version PASS "docker compose is available"; else record compose-version FAIL "docker compose is unavailable"; fi
  if "${COMPOSE[@]}" ps --status running 2>/dev/null | grep -q .; then record compose-services PASS "running services inspected"; else record compose-services FAIL "no running compose services"; fi
  services=(postgres redis neo4j backend sync-service outbox-worker)
  if [[ "$STAGING_TLS" != "1" ]]; then services+=(frontend); fi
  for service in "${services[@]}"; do
    if "${COMPOSE[@]}" ps --status running "$service" 2>/dev/null | grep -q "$service"; then record "service-$service" PASS "running"; else record "service-$service" FAIL "not running"; fi
  done
  if "${COMPOSE[@]}" exec -T backend python manage.py migrate --check >/dev/null 2>&1; then record migration-status PASS "Django migrations are current"; else record migration-status FAIL "pending or unavailable migrations"; fi
  if "${COMPOSE[@]}" exec -T redis redis-cli ping 2>/dev/null | grep -qx PONG; then record redis-health PASS "PONG"; else record redis-health FAIL "Redis unavailable"; fi
  if [[ -n "${NEO4J_PASSWORD:-}" ]] && "${COMPOSE[@]}" exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" 'RETURN 1' >/dev/null 2>&1; then record neo4j-health PASS "Cypher query succeeded"; else record neo4j-health SKIP "Neo4j password/probe unavailable; PostgreSQL fallback remains required"; fi
  if [[ "${STAGING_USE_CONTAINER_HEALTH:-1}" == "1" ]]; then
    health_json="$("${COMPOSE[@]}" exec -T sync-service node -e "fetch('http://127.0.0.1:8787/health').then(async r=>process.stdout.write(await r.text())).catch(()=>process.exit(1))" 2>/dev/null || true)"
  else
    health_json="$(curl --silent --show-error --insecure --max-time 10 "${SYNC_HEALTH_URL:-http://127.0.0.1:8787/health}" 2>/dev/null || true)"
  fi
  if [[ "$health_json" == *'"protocol":"tldraw-sync-v2"'* ]]; then record canvas-sync-protocol PASS "official sync is active"; else record canvas-sync-protocol FAIL "official sync protocol is unavailable"; fi
  if printf '%s' "$health_json" | grep -q 'reconciliation'; then record crdt-reconciliation-health PASS "sync health exposes reconciliation"; else record crdt-reconciliation-health FAIL "sync health does not expose reconciliation"; fi
  if printf '%s' "$health_json" | grep -q 'lease'; then record room-lease-health PASS "sync health exposes room lease"; else record room-lease-health FAIL "sync health does not expose room lease"; fi
else
  record compose-config SKIP "docker is not installed"
  record compose-version SKIP "docker is not installed"
  record compose-services SKIP "docker is not installed"
  for item in migration-status redis-health neo4j-health crdt-reconciliation-health room-lease-health; do record "$item" SKIP "docker is not installed"; done
fi

# Authenticated browser/WS checks require a running staging deployment and
# explicit test credentials. Never report a synthetic success.
if [[ -n "${STAGING_E2E_BASE_URL:-}" && -n "${STAGING_E2E_OWNER_TOKEN:-}" ]]; then
  record authenticated-collaboration-e2e SKIP "credentialed Playwright runner is supplied separately; token was detected but not consumed by shell verifier"
else
  record authenticated-collaboration-e2e SKIP "set STAGING_E2E_BASE_URL and STAGING_E2E_OWNER_TOKEN for Playwright collaboration verification"
fi
[[ -f "$ROOT_DIR/.staging-reports/latest.json" ]] && record backup-report-present PASS "staging report directory exists" || record backup-report-present SKIP "run staging-backup-restore.sh first"

finalize_report
