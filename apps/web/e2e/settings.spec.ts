import { expect, test } from "@playwright/test";

import { createWorkspaceViaApi, loginAsAdmin } from "./helpers";

test("Provider 连通性测试给出结果", async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto("/settings");

  const form = page.getByTestId("provider-form");
  await form.getByLabel("Base URL").fill("http://127.0.0.1:9");
  await form.getByRole("button", { name: "测试连接" }).click();

  await expect(page.getByText("连接失败")).toBeVisible();
});

test("Provider 密钥保存后不回显", async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto("/settings");

  const secret = `sk-e2e-${Date.now()}`;
  const form = page.getByTestId("provider-form");
  await form.getByLabel("名称").fill("E2E Provider");
  await form.getByLabel("Base URL").fill("http://127.0.0.1:9/v1");
  await form.getByRole("textbox", { name: "模型" }).fill("e2e-model");
  await form.getByLabel("API Key").fill(secret);
  await form.getByRole("button", { name: "创建 Provider" }).click();

  const row = page
    .getByTestId("provider-row")
    .filter({ hasText: "E2E Provider" });
  await expect(row).toContainText("已配置密钥");
  await expect(page.getByText(secret)).toHaveCount(0);

  // 重新加载后列表仍然只有掩码状态，没有明文。
  await page.reload();
  await expect(
    page.getByTestId("provider-row").filter({ hasText: "E2E Provider" }),
  ).toContainText("已配置密钥");
  await expect(page.getByText(secret)).toHaveCount(0);
});

test("API Key 明文只在创建时显示一次", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);

  await page.goto("/settings");
  const manager = page.getByTestId("key-manager");
  await manager.getByLabel("工作区").selectOption(workspaceId);
  await manager.getByLabel("Key 名称").fill("E2E Key");
  await manager.getByRole("button", { name: "创建 API Key" }).click();

  const once = page.getByTestId("api-key-once");
  await expect(once).toBeVisible();
  await expect(once).toContainText("只显示一次");
  const rawKey = await once.locator("input").inputValue();
  expect(rawKey.startsWith("mem_")).toBeTruthy();

  await page.reload();
  await expect(page.getByTestId("api-key-once")).toHaveCount(0);
  await expect(page.getByText(rawKey)).toHaveCount(0);
  await expect(
    page.getByTestId("key-row").filter({ hasText: "E2E Key" }),
  ).toBeVisible();
});
