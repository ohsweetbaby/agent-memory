import { expect, test } from "@playwright/test";

import { ADMIN_PASSWORD } from "./env";
import { resetAdmin } from "./helpers";

test.describe.serial("首次启动向导", () => {
  test.beforeAll(() => {
    resetAdmin();
  });

  test("首次启动完成向导并可登录", async ({ page }) => {
    await page.goto("/setup");
    await page.getByLabel("管理员密码", { exact: true }).fill(ADMIN_PASSWORD);
    await page.getByLabel("确认密码", { exact: true }).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "创建管理员" }).click();
    await expect(page).toHaveURL("/");
    await expect(page.getByRole("heading", { name: "概览" })).toBeVisible();
  });

  test("已创建管理员后可通过登录页登录", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("密码", { exact: true }).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page).toHaveURL("/");
    await expect(page.getByRole("heading", { name: "概览" })).toBeVisible();
  });
});
