import { defineConfig, devices } from "@playwright/test";

import {
  API_PORT,
  API_ROOT,
  API_URL,
  UV_BIN,
  WEB_PORT,
  WEB_ROOT,
  WEB_URL,
  apiEnv,
} from "./e2e/env";

/**
 * E2E 使用真实本地 API：
 * - globalSetup 启动独立 Postgres/Redis 容器并执行 Alembic 迁移；
 * - webServer 启动 uvicorn 与 Vite（Vite 把 /api 代理到 uvicorn）。
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  use: {
    baseURL: WEB_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: `"${UV_BIN}" run --frozen uvicorn app.main:create_app --factory --host 127.0.0.1 --port ${API_PORT} --log-level warning`,
      cwd: API_ROOT,
      url: `${API_URL}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: apiEnv(),
    },
    {
      command: `pnpm exec vite --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      cwd: WEB_ROOT,
      url: WEB_URL,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        ...process.env,
        VITE_API_PROXY_TARGET: API_URL,
      },
    },
  ],
});
