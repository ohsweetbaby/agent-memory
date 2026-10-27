import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时 API 默认在 8000；Playwright 通过 VITE_API_PROXY_TARGET 指向 58000。
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
      },
      "/mcp": {
        target: apiTarget,
        changeOrigin: false,
        ws: true,
      },
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
