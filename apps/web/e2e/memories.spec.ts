import { expect, test } from "@playwright/test";

import {
  createMemoryViaApi,
  createWorkspaceViaApi,
  loginAsAdmin,
} from "./helpers";

test("可以搜索并编辑记忆", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);
  await createMemoryViaApi(
    page,
    csrfToken,
    workspaceId,
    "用户对花生过敏",
  );

  await page.goto("/memories");
  await page.getByPlaceholder("搜索记忆").fill("花生");

  const row = page.getByTestId("memory-row").filter({
    hasText: "用户对花生过敏",
  });
  await expect(row).toBeVisible();
  await row.getByTestId("memory-row-content").click();

  const drawer = page.getByTestId("memory-detail");
  await expect(drawer).toBeVisible();
  await drawer.getByLabel("重要性").fill("0.9");
  await drawer.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("已保存")).toBeVisible();

  // 详情与列表都应反映保存后的值。
  await expect(drawer.getByLabel("重要性")).toHaveValue("0.9");
  await expect(
    page.getByTestId("memory-row").filter({ hasText: "用户对花生过敏" }),
  ).toContainText("0.9");
});

test("test_memory_content_is_rendered_as_text", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);
  const marker = `E2E-XSS-${Date.now()}`;
  await createMemoryViaApi(
    page,
    csrfToken,
    workspaceId,
    `<img src=x onerror="window.__xss=1"> ${marker}`,
  );

  await page.goto("/memories");
  await page.getByPlaceholder("搜索记忆").fill(marker);

  await expect(page.getByText('<img src=x onerror="window.__xss=1">')).toBeVisible();
  await expect(page.locator('img[src="x"]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
});
