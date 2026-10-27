import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  ApiError,
  getErrorMessage,
  setCsrfToken,
  UNAUTHORIZED_EVENT,
} from "./api";
import type { AdminUser, LoginResponse, SessionResponse } from "./types";

export const SESSION_QUERY_KEY = ["session"] as const;

export interface SessionState {
  admin: AdminUser | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  setupAdmin: (password: string, username?: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<SessionState | null>(null);

async function fetchSession(): Promise<AdminUser | null> {
  try {
    const me = await api<SessionResponse>("/auth/me");
    return me.kind === "session" ? me.admin : null;
  } catch (error) {
    // 未登录是正常状态，不是错误。
    if (error instanceof ApiError && error.status === 401) {
      return null;
    }
    throw error;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();

  const sessionQuery = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: fetchSession,
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const { refetch } = sessionQuery;

  const refresh = useCallback(async () => {
    await refetch();
  }, [refetch]);

  const login = useCallback(
    async (username: string, password: string) => {
      const result = await api<LoginResponse>("/auth/login", {
        method: "POST",
        json: { username, password },
      });
      setCsrfToken(result.csrf_token);
      queryClient.setQueryData<AdminUser | null>(
        SESSION_QUERY_KEY,
        result.admin,
      );
    },
    [queryClient],
  );

  const setupAdmin = useCallback(
    async (password: string, username = "admin") => {
      await api("/auth/setup", {
        method: "POST",
        json: { username, password },
      });
      await login(username, password);
    },
    [login],
  );

  const logout = useCallback(async () => {
    try {
      await api<void>("/auth/logout", { method: "POST" });
    } catch {
      // 会话可能已过期或 CSRF 已丢失；本地状态照常清理。
    }
    setCsrfToken(null);
    queryClient.setQueryData<AdminUser | null>(SESSION_QUERY_KEY, null);
    queryClient.removeQueries({ queryKey: ["stats"] });
    queryClient.removeQueries({ queryKey: ["jobs"] });
  }, [queryClient]);

  useEffect(() => {
    const handleUnauthorized = () => {
      setCsrfToken(null);
      queryClient.setQueryData<AdminUser | null>(SESSION_QUERY_KEY, null);
      queryClient.removeQueries({ queryKey: ["stats"] });
      queryClient.removeQueries({ queryKey: ["jobs"] });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => {
      window.removeEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
    };
  }, [queryClient]);

  const value = useMemo<SessionState>(
    () => ({
      admin: sessionQuery.data ?? null,
      loading: sessionQuery.isPending,
      error:
        sessionQuery.error && !sessionQuery.data
          ? getErrorMessage(sessionQuery.error)
          : null,
      refresh,
      login,
      setupAdmin,
      logout,
    }),
    [
      sessionQuery.data,
      sessionQuery.isPending,
      sessionQuery.error,
      refresh,
      login,
      setupAdmin,
      logout,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useSession(): SessionState {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error("useSession 必须在 AuthProvider 内使用");
  }
  return context;
}
