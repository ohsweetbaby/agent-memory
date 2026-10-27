import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  api,
  getCsrfToken,
  getErrorMessage,
  setCsrfToken,
  UNAUTHORIZED_EVENT,
} from "./api";

function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("api", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setCsrfToken(null);
  });

  it("带 Cookie 请求并返回解析后的 JSON", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ total_memories: 3 }));

    const result = await api<{ total_memories: number }>("/stats/overview");

    expect(result.total_memories).toBe(3);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/stats/overview");
    expect(init?.credentials).toBe("include");
    expect(init?.method).toBe("GET");
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBeNull();
  });

  it("写请求自动附带 CSRF token 与 JSON 头", async () => {
    setCsrfToken("csrf-token");
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));

    await api("/auth/logout", { method: "POST" });

    const [, init] = fetchMock.mock.calls[0]!;
    const headers = new Headers(init?.headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf-token");
    expect(init?.credentials).toBe("include");
  });

  it("JSON body 会序列化并设置 Content-Type", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));

    await api("/auth/login", {
      method: "POST",
      json: { username: "admin", password: "secret" },
    });

    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.body).toBe('{"username":"admin","password":"secret"}');
    expect(new Headers(init?.headers).get("Content-Type")).toBe(
      "application/json",
    );
  });

  it("把统一错误体转换成 ApiError", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "conflict",
            message: "管理员已存在，请直接登录",
            details: { field: "admin" },
          },
        },
        409,
      ),
    );

    await expect(api("/auth/setup", { method: "POST" })).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      code: "conflict",
      message: "管理员已存在，请直接登录",
      details: { field: "admin" },
    });
  });

  it("204 响应返回 undefined", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(api("/auth/logout", { method: "POST" })).resolves.toBe(
      undefined,
    );
  });

  it("401 会清空 CSRF token", async () => {
    setCsrfToken("csrf-token");
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: "unauthorized", message: "未登录" } }, 401),
    );

    await expect(api("/stats/overview")).rejects.toBeInstanceOf(ApiError);
    expect(getCsrfToken()).toBeNull();
  });

  it("403 csrf_failed 会清空 CSRF token 并广播失效原因", async () => {
    const target = new EventTarget();
    const events: CustomEvent<{ reason?: string }>[] = [];
    target.addEventListener(UNAUTHORIZED_EVENT, (event) => {
      events.push(event as CustomEvent<{ reason?: string }>);
    });
    vi.stubGlobal("window", target);
    setCsrfToken("stale-token");
    fetchMock.mockResolvedValue(
      jsonResponse(
        { error: { code: "csrf_failed", message: "CSRF 校验失败" } },
        403,
      ),
    );

    await expect(
      api("/auth/logout", { method: "POST" }),
    ).rejects.toMatchObject({
      status: 403,
      code: "csrf_failed",
    });
    expect(getCsrfToken()).toBeNull();
    expect(events).toHaveLength(1);
    expect(events[0]?.detail).toEqual({ reason: "csrf_failed" });
  });

  it("登录密码错误不广播全局会话失效", async () => {
    const target = new EventTarget();
    let fired = 0;
    target.addEventListener(UNAUTHORIZED_EVENT, () => {
      fired += 1;
    });
    vi.stubGlobal("window", target);
    fetchMock.mockResolvedValue(
      jsonResponse(
        { error: { code: "unauthorized", message: "用户名或密码错误" } },
        401,
      ),
    );

    await expect(
      api("/auth/login", { method: "POST", json: { password: "wrong" } }),
    ).rejects.toMatchObject({ status: 401 });
    expect(fired).toBe(0);
  });

  it("网络错误给出可读提示", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(api("/stats/overview")).rejects.toMatchObject({
      status: 0,
      code: "network_error",
      message: "无法连接服务器，请确认 API 已启动后重试",
    });
  });

  it("非 JSON 错误响应使用状态码兜底文案", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>Bad Gateway</html>", { status: 502 }),
    );

    const error = await api("/stats/overview").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe("服务器暂时不可用，请稍后重试");
  });

  it("getErrorMessage 兼容未知异常", () => {
    expect(getErrorMessage(new ApiError(400, "bad", "参数错误"))).toBe(
      "参数错误",
    );
    expect(getErrorMessage(new Error("boom"))).toBe("boom");
    expect(getErrorMessage(null)).toBe("操作失败，请稍后重试");
  });
});
