/**
 * Playwright E2E (фаза 20).
 *
 * Тестируется ПРОДАКШЕН-бандл через `vite preview` (vite наследует
 * server.proxy → /api/* уходит на localhost:8001/8002, где live-спек
 * поднимает стаб-бэкенд). Демо- и live-спеки не параллелятся:
 * demo требует выключенного стаба, live — включённого.
 */
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [['list'], ['html', { open: 'never', outputFolder: 'e2e-report' }]]
    : [['list']],
  use: {
    baseURL: 'http://localhost:3000',
    viewport: { width: 1440, height: 900 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run preview',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
