#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep HTTPS as the default for the dedicated staging/production-like path,
# while allowing local development to opt into the plain HTTP compose stack.
export STAGING_TLS="${STAGING_TLS:-1}"
if [[ "$STAGING_TLS" == "1" ]]; then
  COMPOSE=(docker compose -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/deploy/docker-compose.prod.yml")
else
  COMPOSE=(docker compose -f "$ROOT_DIR/docker-compose.yml")
fi
REPORT_DIR="${PROJECTOC_STAGING_REPORT_DIR:-$ROOT_DIR/.staging-reports}"
mkdir -p "$REPORT_DIR"
REPORT_JSON="$REPORT_DIR/latest.json"
REPORT_MD="$REPORT_DIR/latest.md"
# Keep staging containers and volumes isolated from the developer compose stack.
# This also prevents a local Neo4j password/volume mismatch from blocking a
# clean acceptance run.
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-projectoc-staging}"
# Let backup/restore helpers select the exact staging overlay when invoked as
# child processes. Normal developer backups keep their original compose path.
export PROJECTOC_STAGING_COMPOSE=1

# The staging scripts are intentionally runnable on a clean checkout.  These
# values are disposable local defaults only; production compose still requires
# explicit secrets through deploy/.env.production.example.
if [[ "${STAGING_ALLOW_LOCAL_DEFAULTS:-1}" == "1" ]]; then
  export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-projectoc-staging-postgres-change-me}"
  export NEO4J_PASSWORD="${NEO4J_PASSWORD:-projectoc-staging-neo4j-change-me}"
  export DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-projectoc-staging-django-secret-change-me}"
  # Keep a disposable but stable local Fernet key across staging restarts so
  # encrypted workspace credentials remain decryptable without requiring a
  # production secret to be checked into the repository.
  if [[ -z "${WORKSPACE_CREDENTIALS_KEY:-}" ]]; then
    local_credentials_file="${PROJECTOC_STAGING_CREDENTIALS_FILE:-$ROOT_DIR/.staging/workspace-credentials.key}"
    mkdir -p "$(dirname "$local_credentials_file")"
    if [[ ! -s "$local_credentials_file" ]]; then
      umask 077
      python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())' > "$local_credentials_file"
    fi
    export WORKSPACE_CREDENTIALS_KEY="$(tr -d '\r\n' < "$local_credentials_file")"
  else
    export WORKSPACE_CREDENTIALS_KEY
  fi
  export DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1,backend}"
  export SYNC_TICKET_SECRET="${SYNC_TICKET_SECRET:-projectoc-staging-ticket-secret-change-me-123456}"
  export SYNC_INTERNAL_SECRET="${SYNC_INTERNAL_SECRET:-projectoc-staging-internal-secret-change-me-123456}"
  export TLDRAW_SCHEMA_VERSION="${TLDRAW_SCHEMA_VERSION:-oc-tldraw-2}"
  if [[ "$STAGING_TLS" == "1" ]]; then
    export CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-https://localhost}"
    export SYNC_SERVICE_URL="${SYNC_SERVICE_URL:-wss://localhost/rooms/}"
    export SYNC_ALLOWED_ORIGINS="${SYNC_ALLOWED_ORIGINS:-https://localhost}"
    export DJANGO_DEBUG="${DJANGO_DEBUG:-0}"
    export SYNC_REQUIRE_ORIGIN="${SYNC_REQUIRE_ORIGIN:-1}"
  else
    # The local staging UI is served on 5174 by default. Keep both the
    # Vite default and the configured port trusted so switching ports does
    # not produce Django's "Origin checking failed" CSRF error.
    export CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174}"
    export CSRF_TRUSTED_ORIGINS="${CSRF_TRUSTED_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174}"
    export SYNC_SERVICE_URL="${SYNC_SERVICE_URL:-ws://localhost:8787}"
    export SYNC_ALLOWED_ORIGINS="${SYNC_ALLOWED_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174}"
    export DJANGO_DEBUG="${DJANGO_DEBUG:-1}"
    export DJANGO_SECURE_SSL_REDIRECT="${DJANGO_SECURE_SSL_REDIRECT:-0}"
    export SYNC_REQUIRE_ORIGIN="${SYNC_REQUIRE_ORIGIN:-0}"
    export STAGING_BASE_URL="${STAGING_BASE_URL:-http://localhost:5173}"
    export SYNC_HEALTH_URL="${SYNC_HEALTH_URL:-http://127.0.0.1:8787/health}"
  fi
fi
ensure_staging_tls() {
  if [[ "${STAGING_TLS:-1}" == "1" ]]; then
    export TLS_CERT_DIR="${TLS_CERT_DIR:-$($ROOT_DIR/deploy/staging-tls.sh)}"
    export STAGING_BASE_URL="${STAGING_BASE_URL:-https://localhost}"
    export SYNC_HEALTH_URL="${SYNC_HEALTH_URL:-http://127.0.0.1:8787/health}"
  fi
}
run() { echo "+ $*"; "$@"; }
record() { local name="$1" status="$2" detail="${3:-}"; python3 - "$REPORT_JSON" "$name" "$status" "$detail" <<'PY'
import json, pathlib, sys, os
p=pathlib.Path(sys.argv[1]); data=json.loads(p.read_text()) if p.exists() else {"started_at":"","checks":[]}
data["checks"].append({"name":sys.argv[2],"status":sys.argv[3],"detail":sys.argv[4]})
p.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
PY
}

finalize_report() {
  python3 - "$REPORT_JSON" "$REPORT_MD" <<'PY'
import json, pathlib, sys, os
p=pathlib.Path(sys.argv[1]); data=json.loads(p.read_text()) if p.exists() else {"checks": []}
counts={k: sum(1 for x in data.get("checks",[]) if x.get("status")==k) for k in ("PASS","FAIL","SKIP")}
p0_names = {"projectoc-https-health", "https-endpoint", "https-redirect", "wss-edge-reachable", "django-debug", "allowed-hosts", "sync-secrets", "websocket-origin", "official-crdt-enabled", "tldraw-schema", "migration-status", "compose-services", "crdt-reconciliation-health", "room-lease-health"}
p1_names = {"sync-health", "secure-redirect-configured", "redis-health", "neo4j-health", "backup-report-present", "authenticated-collaboration-e2e"}
def gate(names):
    rows = [x for x in data.get("checks", []) if x.get("name") in names]
    return bool(rows) and all(x.get("status") == "PASS" for x in rows)
license_ok = any(x.get("name") == "tldraw-license-configured" and x.get("status") == "PASS" for x in data.get("checks", []))
data["p0_passed"] = gate(p0_names)
data["p1_passed"] = gate(p1_names)
data["license_configured"] = license_ok
data["production_collaboration_enabled"] = bool(data["p0_passed"] and data["p1_passed"] and license_ok and os.environ.get("PRODUCTION_COLLABORATION_ENABLE") == "1")
data["counts"]=counts; data["finished_at"] = __import__("datetime").datetime.now().astimezone().isoformat(); p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
lines=["# ProjectOC staging 验收报告","",f"开始时间：{data.get('started_at','')}",f"完成时间：{data.get('finished_at','')}","",f"P0：{'PASS' if data['p0_passed'] else 'FAIL'}；P1：{'PASS' if data['p1_passed'] else 'FAIL'}；license_configured：{'PASS' if data['license_configured'] else 'SKIP'}；production_collaboration_enabled：{'true' if data['production_collaboration_enabled'] else 'false'}","", "| 检查 | 状态 | 详情 |", "|---|---|---|"]
for c in data.get("checks",[]): lines.append(f"| {c.get('name','')} | {c.get('status','')} | {str(c.get('detail','')).replace('|','\\|')} |")
lines += ["", f"汇总：PASS={counts['PASS']} FAIL={counts['FAIL']} SKIP={counts['SKIP']}"]
pathlib.Path(sys.argv[2]).write_text("\n".join(lines)+"\n")
print(json.dumps(data,ensure_ascii=False,indent=2))
PY
}
