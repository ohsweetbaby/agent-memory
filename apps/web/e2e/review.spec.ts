import { expect, test } from "@playwright/test";

import {
  clearPendingReviewItems,
  loginAsAdmin,
  seedPendingReview,
} from "./helpers";

test("待审候选可通过并生成记忆", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  // 概览等其他用例也会播种待审项；清空 pending 才能断言队列归零。
  clearPendingReviewItems();
  await seedPendingReview(page, csrfToken, 1);

  await page.goto("/review");
  const item = page.getByTestId("review-item").filter({
    hasText: "E2E 待审候选 1",
  });
  await expect(item).toBeVisible();

  await item.getByRole("button", { name: "通过" }).click();
  await expect(page.getByText("待审队列为空")).toBeVisible();

  // 通过后确实落库为记忆，而不只是从前端列表移除。
  const created = await page.request.get(
    `/api/v1/memories?q=${encodeURIComponent("E2E 待审候选 1")}`,
  );
  expect(created.ok()).toBeTruthy();
  const body = (await created.json()) as {
    items: Array<{ content: string }>;
  };
  expect(body.items.map((memory) => memory.content)).toContain(
    "E2E 待审候选 1",
  );
});

test("批量通过会使用卡片内编辑后的内容", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  clearPendingReviewItems();
  const workspaceId = await seedPendingReview(page, csrfToken, 1);
  const edited = `E2E 批量编辑后的候选 ${Date.now()}`;

  await page.goto("/review");
  const item = page.getByTestId("review-item").filter({
    hasText: "E2E 待审候选 1",
  });
  await expect(item).toBeVisible();

  await item.getByLabel("候选正文").fill(edited);
  await page.getByRole("checkbox", { name: "选择待审项" }).check();
  await expect(
    page.getByText("已编辑的候选会按编辑后内容通过"),
  ).toBeVisible();

  await page.getByRole("button", { name: "批量通过" }).click();
  await expect(page.getByText("待审队列为空")).toBeVisible();

  const editedResult = await page.request.get(
    `/api/v1/memories?workspace_id=${workspaceId}&q=${encodeURIComponent(edited)}`,
  );
  expect(editedResult.ok()).toBeTruthy();
  const editedBody = (await editedResult.json()) as {
    items: Array<{ content: string }>;
  };
  expect(editedBody.items.map((memory) => memory.content)).toContain(edited);

  // 原候选不应再以未编辑内容入库。
  const originalResult = await page.request.get(
    `/api/v1/memories?workspace_id=${workspaceId}&q=${encodeURIComponent(
      "E2E 待审候选 1",
    )}`,
  );
  expect(originalResult.ok()).toBeTruthy();
  const originalBody = (await originalResult.json()) as {
    items: Array<{ content: string }>;
  };
  expect(originalBody.items).toHaveLength(0);
});
