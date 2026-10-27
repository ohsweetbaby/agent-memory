/// <reference types="node" />

/** E2E 环境常量：真实本地 API + 独立 Postgres/Redis 容器。 */
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** apps/web */
export const WEB_ROOT = path.resolve(HERE, "..");
/** 仓库根目录 */
export const REPO_ROOT = path.resolve(WEB_ROOT, "..", "..");
/** API 源码目录（uv workspace，含 alembic.ini） */
export const API_ROOT = path.resolve(REPO_ROOT, "apps", "api");

/**
 * uv 不一定在 PATH 上（本机安装在用户 Scripts 目录），优先 UV_BIN。
 */
function resolveUv(): string {
  if (process.env.UV_BIN) {
    return process.env.UV_BIN;
  }
  const candidates = [
    process.env.APPDATA
      ? path.join(
          process.env.APPDATA,
          "Python",
          "Python314",
          "Scripts",
          "uv.exe",
        )
      : undefined,
    process.platform === "win32"
      ? path.join(
          process.env.USERPROFILE ?? "",
          "AppData",
          "Roaming",
          "Python",
          "Python314",
          "Scripts",
          "uv.exe",
        )
      : undefined,
  ];
  return candidates.find((item) => item && existsSync(item)) ?? "uv";
}

export const UV_BIN = resolveUv();

export const API_PORT = Number(process.env.E2E_API_PORT ?? 58000);
export const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5173);

export const API_URL = `http://127.0.0.1:${API_PORT}`;
export const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;

export const PG_CONTAINER = "am-e2e-postgres";
export const REDIS_CONTAINER = "am-e2e-redis";
export const PG_PORT = Number(process.env.E2E_PG_PORT ?? 55432);
export const REDIS_PORT = Number(process.env.E2E_REDIS_PORT ?? 56379);

export const PG_USER = "agent_memory";
export const PG_PASSWORD = "agent_memory";
export const PG_DATABASE = "agent_memory_e2e";

export const DATABASE_URL = `postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@127.0.0.1:${PG_PORT}/${PG_DATABASE}`;
export const REDIS_URL = `redis://127.0.0.1:${REDIS_PORT}/0`;

export const SECRET_KEY = "e2e-only-secret-key";
export const ADMIN_USERNAME = "admin";
export const ADMIN_PASSWORD = "p@ssw0rd-123";

/** API 进程的独立环境，避免读入仓库 .env 的真实配置。 */
export function apiEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    ENVIRONMENT: "test",
    LOG_LEVEL: "WARNING",
    SECRET_KEY,
    DATABASE_URL,
    REDIS_URL,
    MCP_ENABLED: "false",
    EMBEDDING_PROVIDER: "fake",
    LLM_API_KEY: "",
  };
}
