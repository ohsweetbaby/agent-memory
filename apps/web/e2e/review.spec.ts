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
