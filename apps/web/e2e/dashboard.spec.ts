import { expect, test } from "@playwright/test";

import { loginAsAdmin, seedMemories, seedPendingReview } from "./helpers";

test("概览展示记忆计数", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  const { ids } = await seedMemories(page, csrfToken, 3);
  await seedPendingReview(page, csrfToken);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "概览" })).toBeVisible();

  const total = page.getByTestId("stat-total-memories");
  await expect(total).toBeVisible();
  await expect(total).toHaveText(/^\d+$/);
  expect(Number(await total.textContent())).toBeGreaterThanOrEqual(ids.length);

  await expect(page.getByTestId("stat-pending-review")).toHaveText("1");
  await expect(page.getByTestId("stat-pending-review")).toBeVisible();
});
