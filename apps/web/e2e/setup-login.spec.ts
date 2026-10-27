import { expect, test } from "@playwright/test";

import { ADMIN_PASSWORD, WEB_URL } from "./env";
import { loginAsAdmin, resetAdmin } from "./helpers";

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

  test("新标签页没有 sessionStorage 时水合 CSRF 并可正常退出", async ({
    page,
    browser,
  }) => {
    // 先建立持久 Cookie 会话（不带任何前端 sessionStorage）。
    await loginAsAdmin(page);
    const cookies = await page.context().cookies();

    const freshContext = await browser.newContext({ baseURL: WEB_URL });
    try {
      await freshContext.addCookies(cookies);
      const freshPage = await freshContext.newPage();
      await freshPage.goto("/");
      await expect(
        freshPage.getByRole("heading", { name: "概览" }),
      ).toBeVisible();

      // /auth/me 必须把 CSRF token 水合进新标签页。
      const hydrated = await freshPage.evaluate(() =>
        window.sessionStorage.getItem("am.csrf-token"),
      );
      expect(hydrated).toBeTruthy();

      await freshPage.getByRole("button", { name: "退出登录" }).click();
      await expect(freshPage).toHaveURL("/login");

      // 退出必须真正吊销服务端 Cookie，而不是前端假退出。
      const me = await freshContext.request.get("/api/v1/auth/me");
      expect(me.status()).toBe(401);
    } finally {
      await freshContext.close();
    }
  });

  test("CSRF 失效时提示重新登录且不再静默失败", async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "概览" })).toBeVisible();

    // 模拟持久会话丢失 CSRF：只改 logout 请求头，接口仍走真实后端。
    await page.route("**/api/v1/auth/logout", async (route) => {
      const headers = Object.fromEntries(
        Object.entries(route.request().headers()).filter(
          ([key]) => key.toLowerCase() !== "x-csrf-token",
        ),
      );
      headers["x-csrf-token"] = "expired-token";
      await route.continue({ headers });
    });

    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page).toHaveURL("/login");
    await expect(
      page.getByText("会话令牌已失效，请重新登录"),
    ).toBeVisible();
  });
});
