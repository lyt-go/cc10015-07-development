import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 300_000,
  expect: { timeout: 10_000 },
  // 后端是单例内存仓库（app/store.py），所有测试共享同一份数据；
  // 并行 worker 会互相污染行计数与状态，E2E 必须串行执行。
  workers: 1,
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:5173',
    browserName: 'chromium',
    headless: true,
    actionTimeout: 10_000,
  },
  reporter: 'line',
})
