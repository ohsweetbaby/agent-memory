import { expect, test } from "@playwright/test";

import {
  createMemoryViaApi,
  createWorkspaceViaApi,
  loginAsAdmin,
} from "./helpers";

test("调试台展示分路召回得分", async ({ page }) => {
  const csrfToken = await loginAsAdmin(page);
  const workspaceId = await createWorkspaceViaApi(page, csrfToken);
  // 管理台未携带 end_user_id，因此用 global 作用域确保四路召回可见。
  await createMemoryViaApi(
    page,
    csrfToken,
    workspaceId,
    "用户对花生过敏，外出就餐需要避开花生制品",
    "semantic",
    "global",
    null,
  );

  await page.goto("/playground");
  await page.getByTestId("playground-workspace").selectOption(workspaceId);
  await page.getByPlaceholder("输入检索语句").fill("花生过敏");
  await page.getByRole("button", { name: "检索" }).click();

  await expect(page.getByTestId("channel-vector")).toBeVisible();
  await expect(page.getByTestId("channel-bm25")).toBeVisible();
  await expect(page.getByTestId("channel-bm25")).toContainText("花生过敏");
});
