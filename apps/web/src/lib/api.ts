/**
 * REST 客户端：Cookie 会话、CSRF 双提交与统一错误模型。
 *
 * - 浏览器自动携带 HttpOnly 会话 Cookie（`credentials: "include"`）；
 * - 登录响应里的 CSRF token 只保存在 sessionStorage 与内存中，不落明文密码/Key；
 * - 所有非 2xx 响应都转换成 `ApiError{code,message,details}`，供页面直接展示。
 */

export const API_PREFIX = "/api/v1";
export const UNAUTHORIZED_EVENT = "am:unauthorized";

const CSRF_STORAGE_KEY = "am.csrf-token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
/** 这些端点上的 401 是“未登录/密码错误”，不应广播成全局会话失效。 */
const UNAUTHENTICATED_ENDPOINTS = new Set(["/auth/me", "/auth/login"]);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function readStoredCsrfToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.sessionStorage.getItem(CSRF_STORAGE_KEY);
  } catch {
    return null;
  }
}

let csrfToken: string | null = readStoredCsrfToken();

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
  if (typeof window === "undefined") {
    return;
  }
  try {
    if (token === null) {
      window.sessionStorage.removeItem(CSRF_STORAGE_KEY);
    } else {
      window.sessionStorage.setItem(CSRF_STORAGE_KEY, token);
    }
  } catch {
    // 隐私模式下 sessionStorage 可能不可写；内存 token 仍然有效。
  }
}

export interface ApiRequestInit extends Omit<RequestInit, "body"> {
  /** 直接传入的原始 body（FormData、字符串等）。 */
  body?: BodyInit | null;
  /** JSON body；与 `body` 二选一。 */
  json?: unknown;
}

interface ErrorShape {
  error?: {
    code?: unknown;
    message?: unknown;
    details?: unknown;
  };
}

function parseError(payload: unknown): {
  code?: string;
  message?: string;
  details?: unknown;
} {
  if (payload === null || typeof payload !== "object") {
    return {};
  }
  const shape = payload as ErrorShape;
  if (shape.error === null || typeof shape.error !== "object") {
    return {};
  }
  return {
    code: typeof shape.error.code === "string" ? shape.error.code : undefined,
    message:
      typeof shape.error.message === "string" ? shape.error.message : undefined,
    details: shape.error.details,
  };
}

function defaultMessage(status: number): string {
  if (status === 401) return "登录状态已失效，请重新登录";
  if (status === 403) return "没有权限执行该操作";
  if (status === 404) return "请求的资源不存在";
  if (status === 409) return "当前状态下无法完成该操作";
  if (status === 422) return "提交的内容不符合要求";
  if (status === 429) return "请求过于频繁，请稍后再试";
  if (status >= 500) return "服务器暂时不可用，请稍后重试";
  return `请求失败（HTTP ${status}）`;
}

function notifyUnauthorized(reason: "unauthorized" | "csrf_failed"): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent(UNAUTHORIZED_EVENT, { detail: { reason } }),
    );
  }
}

export async function api<T>(
  path: string,
  init: ApiRequestInit = {},
): Promise<T> {
  const { json, body: rawBody, headers: rawHeaders, ...rest } = init;
  const method = (rest.method ?? "GET").toUpperCase();
  const headers = new Headers(rawHeaders);

  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  let body = rawBody;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }

  if (!SAFE_METHODS.has(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }

  let response: Response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      ...rest,
      method,
      headers,
      body,
      credentials: "include",
    });
  } catch {
    throw new ApiError(
      0,
      "network_error",
      "无法连接服务器，请确认 API 已启动后重试",
    );
  }

  if (response.status === 401) {
    setCsrfToken(null);
    // /auth/me 的 401 是“未登录”的正常探测、/auth/login 是密码错误，
    // 都不触发全局跳转。
    if (!UNAUTHENTICATED_ENDPOINTS.has(path)) {
      notifyUnauthorized("unauthorized");
    }
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text !== "") {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const parsed = parseError(payload);
    if (response.status === 403 && parsed.code === "csrf_failed") {
      // 持久 Cookie 会话刷新后若丢了 CSRF token，所有写操作都会 403；
      // 统一清本地会话并提示重新登录，避免静默失败。
      setCsrfToken(null);
      notifyUnauthorized("csrf_failed");
    }
    throw new ApiError(
      response.status,
      parsed.code ?? `http_${response.status}`,
      parsed.message ?? defaultMessage(response.status),
      parsed.details,
    );
  }

  return payload as T;
}

/** 把任意异常转换成可直接展示给用户的中文文案。 */
export function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  if (typeof error === "string" && error) {
    return error;
  }
  return "操作失败，请稍后重试";
}
