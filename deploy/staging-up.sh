#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/deploy/staging-common.sh"
ensure_staging_tls
mkdir -p "$REPORT_DIR"
printf '{"started_at":"%s","checks":[]}' "$(date -Is)" > "$REPORT_JSON"
if command -v docker >/dev/null 2>&1; then
  if run "${COMPOSE[@]}" up -d --build; then record compose-up PASS "staging services started"; else record compose-up FAIL "docker compose failed"; fi
  if "${COMPOSE[@]}" ps >/dev/null 2>&1; then record compose-inspection PASS "compose status collected"; else record compose-inspection FAIL "cannot inspect compose status"; fi
else
  record compose-up SKIP "docker is not installed; scripts can still validate configuration"
fi
if [[ "$STAGING_TLS" == "1" ]]; then
  record staging-tls PASS "TLS_CERT_DIR=$TLS_CERT_DIR (local CA/self-signed; not a production CA claim)"
else
  record staging-tls SKIP "HTTP development mode; HTTPS/TLS disabled"
fi
if [[ -n "${VITE_TLDRAW_LICENSE_KEY:-}" ]]; then
  record license-configured PASS "license_configured"
else
  record license-configured SKIP "manual TLDraw license gate; set VITE_TLDRAW_LICENSE_KEY before production"
fi
finalize_report
printf 'staging started\nreport=%s\n' "$REPORT_DIR"
