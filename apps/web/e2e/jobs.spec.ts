import { expect, test } from "@playwright/test";

import {
  createWorkspaceViaApi,
  loginAsAdmin,
  seedCompletedJob,
} from "./helpers";

test("巩固任务可 dry-run 并展示差异", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);

  await page.goto("/jobs");
  await page.getByTestId("jobs-workspace").selectOption(workspaceId);
  await page.getByRole("button", { name: "巩固 dry-run" }).click();

  await expect(page.getByTestId("dry-run-diff")).toContainText("将新增");
  await expect(page.getByTestId("dry-run-diff")).toContainText("dry-run");
});

test("任务回滚需要二次确认", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);
  const jobId = seedCompletedJob(workspaceId);

  await page.goto("/jobs");
  const row = page.locator(`[data-job-id="${jobId}"]`);
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "回滚" }).click();

  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("确认回滚");

  // 取消不会调用回滚接口，任务仍保持完成状态。
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row).toContainText("已完成");
});
