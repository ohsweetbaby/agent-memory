import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";

import type { Page } from "@playwright/test";

import {
  ADMIN_PASSWORD,
  ADMIN_USERNAME,
  PG_CONTAINER,
  PG_DATABASE,
  PG_USER,
} from "./env";

const API = "/api/v1";

function psql(sql: string): void {
  execFileSync(
    "docker",
    [
      "exec",
      "-i",
      PG_CONTAINER,
      "psql",
      "-v",
      "ON_ERROR_STOP=1",
      "-q",
      "-U",
      PG_USER,
      "-d",
      PG_DATABASE,
    ],
    { input: sql, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] },
  );
}

function sqlLiteral(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/** 清空管理员，使首次向导用例可以从初始状态开始。 */
export function resetAdmin(): void {
  psql("DELETE FROM admin_user;");
}

/**
 * 确保管理员存在并登录，返回本次会话的 CSRF token。
 *
 * 通过 `page.request` 调用 API，Cookie 会写入浏览器上下文，
 * 因此后续 `page.goto('/')` 直接处于登录态。
 */
export async function loginAsAdmin(page: Page): Promise<string> {
  const setup = await page.request.post(`${API}/auth/setup`, {
    data: { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
  });
  if (![201, 409].includes(setup.status())) {
    throw new Error(`创建管理员失败：${setup.status()} ${await setup.text()}`);
  }

  const login = await page.request.post(`${API}/auth/login`, {
    data: { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
  });
  if (!login.ok()) {
    throw new Error(`登录失败：${login.status()} ${await login.text()}`);
  }
  const body = (await login.json()) as { csrf_token: string };
  return body.csrf_token;
}

export async function createWorkspaceViaApi(
  page: Page,
  csrfToken: string,
  name = `E2E 工作区 ${randomUUID().slice(0, 8)}`,
): Promise<string> {
  const response = await page.request.post(`${API}/workspaces`, {
    headers: { "X-CSRF-Token": csrfToken },
    data: { name },
  });
  if (!response.ok()) {
    throw new Error(`创建工作区失败：${response.status()} ${await response.text()}`);
  }
  const body = (await response.json()) as { id: string };
  return body.id;
}

/** 通过 REST API 创建一条记忆。 */
export async function createMemoryViaApi(
  page: Page,
  csrfToken: string,
  workspaceId: string,
  content: string,
  type = "semantic",
): Promise<string> {
  const response = await page.request.post(`${API}/memories`, {
    headers: { "X-CSRF-Token": csrfToken },
    data: {
      workspace_id: workspaceId,
      content,
      type,
      scope: "user",
      scope_id: "e2e-user",
      importance: 0.6,
      dedupe: false,
    },
  });
  if (!response.ok()) {
    throw new Error(`创建记忆失败：${response.status()} ${await response.text()}`);
  }
  const body = (await response.json()) as { id: string };
  return body.id;
}

/** 批量播种记忆，供概览与列表用例使用。 */
export async function seedMemories(
  page: Page,
  csrfToken: string,
  count = 3,
): Promise<{ workspaceId: string; ids: string[] }> {
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);
  const types = ["semantic", "episodic", "procedural", "working", "perceptual"];
  const ids: string[] = [];
  for (let index = 0; index < count; index += 1) {
    ids.push(
      await createMemoryViaApi(
        page,
        csrfToken,
        workspaceId,
        `E2E 种子记忆 ${index + 1}：用户偏好第 ${index + 1} 项`,
        types[index % types.length],
      ),
    );
  }
  return { workspaceId, ids };
}

/** 删除所有待处理候选，保证“队列归零”断言不受其他用例播种影响。 */
export function clearPendingReviewItems(): void {
  psql("DELETE FROM review_item WHERE status = 'pending';");
}

/**
 * 待审项没有公开的创建端点（正常由抽取管线产生），
 * 这里直接向测试库插入，模拟低置信度候选。
 */
export async function seedPendingReview(
  page: Page,
  csrfToken: string,
  count = 1,
): Promise<string> {
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);
  const values: string[] = [];
  for (let index = 0; index < count; index += 1) {
    const candidate = JSON.stringify({
      content: `E2E 待审候选 ${index + 1}`,
      type: "semantic",
      importance: 0.6,
      confidence: 0.5,
      entities: [],
      scope: "user",
      scope_id: "e2e-user",
    });
    const evidence = JSON.stringify({ quote: "用户可能喜欢这样", source: "e2e" });
    values.push(
      `(${sqlLiteral(randomUUID())}, ${sqlLiteral(workspaceId)}, ${sqlLiteral(
        candidate,
      )}::jsonb, ${sqlLiteral(evidence)}::jsonb, 0.5, 'pending', now())`,
    );
  }
  psql(
    `INSERT INTO review_item (id, workspace_id, candidate, evidence, confidence, status, created_at) VALUES ${values.join(
      ", ",
    )};`,
  );
  return workspaceId;
}
