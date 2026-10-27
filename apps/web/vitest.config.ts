import { defineConfig } from "vitest/config";

// 前端单元测试只跑 src 下的 *.test.ts(x)；e2e/ 由 Playwright（pnpm test:e2e）执行。
export default defineConfig({
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "node",
  },
});
