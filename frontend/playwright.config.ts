import { defineConfig, devices } from '@playwright/test'

/**
 * Browser-level collaboration checks use isolated local services.  The
 * backend gets a throw-away SQLite database and the sync service uses the
 * same ticket secrets as Django, so the test exercises the real auth → ticket
 * → websocket path rather than mocking collaboration.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['line'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:5175',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: `../.venv/bin/python -c 'from pathlib import Path; p=Path("/tmp/projectoc-playwright.sqlite3"); p.unlink(missing_ok=True)' && SQLITE_DB_PATH=/tmp/projectoc-playwright.sqlite3 ../.venv/bin/python manage.py migrate --noinput && SQLITE_DB_PATH=/tmp/projectoc-playwright.sqlite3 DJANGO_DEBUG=1 CSRF_TRUSTED_ORIGINS=http://127.0.0.1:5175 SYNC_SERVICE_URL=ws://127.0.0.1:8788 SYNC_TICKET_SECRET=projectoc-playwright-ticket-secret SYNC_INTERNAL_SECRET=projectoc-playwright-internal-secret ../.venv/bin/python manage.py runserver 127.0.0.1:8124 --noreload`,
      cwd: '../backend',
      url: 'http://127.0.0.1:8124/health/',
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: 'PORT=8788 SYNC_CONTROL_PORT=8789 DJANGO_INTERNAL_URL=http://127.0.0.1:8124 SYNC_TICKET_SECRET=projectoc-playwright-ticket-secret SYNC_INTERNAL_SECRET=projectoc-playwright-internal-secret SYNC_ALLOWED_ORIGINS=http://127.0.0.1:5175 SYNC_REQUIRE_ORIGIN=1 node ../frontend/e2e/sync-server-wrapper.mjs',
      cwd: '../sync-service',
      url: 'http://127.0.0.1:8788/health',
      timeout: 60_000,
      reuseExistingServer: false,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5175',
      cwd: '.',
      env: { ...process.env, VITE_API_PROXY_TARGET: 'http://127.0.0.1:8124' },
      url: 'http://127.0.0.1:5175/',
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
})
