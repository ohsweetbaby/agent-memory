import { execFileSync } from "node:child_process";

import {
  API_ROOT,
  DATABASE_URL,
  PG_CONTAINER,
  PG_DATABASE,
  PG_PASSWORD,
  PG_PORT,
  PG_USER,
  REDIS_CONTAINER,
  REDIS_PORT,
  SECRET_KEY,
  UV_BIN,
} from "./env";

function sleep(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function docker(args: string[]): string {
  return execFileSync("docker", args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 120_000,
  });
}

function removeContainer(name: string): void {
  try {
    docker(["rm", "-f", name]);
  } catch {
    // 容器本来就不存在。
  }
}

function waitUntil(label: string, probe: () => boolean, timeoutMs = 90_000): void {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      if (probe()) return;
    } catch (error) {
      lastError = error;
    }
    sleep(250);
  }
  throw new Error(
    `等待 ${label} 就绪超时${lastError ? `：${String(lastError)}` : ""}`,
  );
}

function waitForPostgres(): void {
  waitUntil("Postgres", () => {
    const output = docker([
      "exec",
      PG_CONTAINER,
      "pg_isready",
      "-U",
      PG_USER,
      "-d",
      PG_DATABASE,
    ]);
    return output.includes("accepting connections");
  });
}

function waitForRedis(): void {
  waitUntil("Redis", () => {
    const output = docker(["exec", REDIS_CONTAINER, "redis-cli", "ping"]);
    return output.trim() === "PONG";
  });
}

function runSql(sql: string): void {
  execFileSync(
    "docker",
    [
      "exec",
      "-i",
      PG_CONTAINER,
      "psql",
      "-v",
      "ON_ERROR_STOP=1",
      "-U",
      PG_USER,
      "-d",
      PG_DATABASE,
    ],
    { input: sql, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] },
  );
}

function runMigrations(): void {
  execFileSync(
    UV_BIN,
    [
      "run",
      "--frozen",
      "alembic",
      "-c",
      "alembic.ini",
      "upgrade",
      "head",
    ],
    {
      cwd: API_ROOT,
      env: { ...process.env, DATABASE_URL, SECRET_KEY },
      stdio: "inherit",
      timeout: 180_000,
    },
  );
}

/**
 * 每次 E2E 都重建独立测试库，执行迁移并清空管理员，
 * 保证首次向导用例可从“无管理员”状态开始。
 */
export default function globalSetup(): void {
  removeContainer(PG_CONTAINER);
  removeContainer(REDIS_CONTAINER);

  docker([
    "run",
    "-d",
    "--name",
    PG_CONTAINER,
    "-e",
    `POSTGRES_USER=${PG_USER}`,
    "-e",
    `POSTGRES_PASSWORD=${PG_PASSWORD}`,
    "-e",
    `POSTGRES_DB=${PG_DATABASE}`,
    "-p",
    `127.0.0.1:${PG_PORT}:5432`,
    "postgres:16-alpine",
  ]);

  docker([
    "run",
    "-d",
    "--name",
    REDIS_CONTAINER,
    "-p",
    `127.0.0.1:${REDIS_PORT}:6379`,
    "redis:7-alpine",
  ]);

  waitForPostgres();
  waitForRedis();
  runMigrations();

  runSql("DELETE FROM admin_user;");
  docker(["exec", REDIS_CONTAINER, "redis-cli", "FLUSHALL"]);
}
