# Agent Memory Web 管理台

React 18 + Vite + TypeScript + Tailwind CSS（手写 shadcn/ui 等价组件）+ TanStack Query + React Router。

## 开发

```bash
# 终端 1：API（默认 8000）
uv run --frozen uvicorn app.main:create_app --factory --reload

# 终端 2：前端（默认 5173，/api 代理到 http://127.0.0.1:8000）
pnpm --dir apps/web dev
```

如需指向其他 API：`VITE_API_PROXY_TARGET=http://127.0.0.1:58000 pnpm dev`。

## 构建与单元测试

```bash
pnpm --dir apps/web test     # vitest
pnpm --dir apps/web build    # tsc --noEmit && vite build
```

## E2E（真实本地 API）

`playwright.config.ts` 的 `globalSetup` 会：

1. 重建独立容器 `am-e2e-postgres`（127.0.0.1:55432）与 `am-e2e-redis`（127.0.0.1:56379）；
2. 执行 Alembic 迁移并清空管理员；
3. 由 `webServer` 启动 uvicorn（127.0.0.1:58000）与 Vite（127.0.0.1:5173）。

```bash
# 首次需要浏览器（npmmirror 镜像）
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ \
  pnpm --dir apps/web exec playwright install chromium

pnpm --dir apps/web test:e2e
```

- 依赖本地已有 `postgres:16-alpine`、`redis:7-alpine` 镜像，不访问 Docker Hub；
- `UV_BIN` 可指定 `uv` 可执行文件（Windows 默认查找用户 Python Scripts 目录）；
- 端口可通过 `E2E_API_PORT`、`E2E_WEB_PORT`、`E2E_PG_PORT`、`E2E_REDIS_PORT` 覆盖；
- 测试结束后 `globalTeardown` 删除两个测试容器；
- 待审种子数据没有公开创建端点，`e2e/helpers.ts` 通过 `docker exec psql` 直接插入测试库。

## 当前范围（任务 17）

已实现：首次向导、登录、会话态保护、概览（总量/类型/作用域/待审/任务健康）与八个路由骨架。
记忆浏览器、待审队列、任务中心、设置和检索调试台在后续任务中接入。
