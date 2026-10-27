/** 与后端 REST 响应对应的最小类型集合。 */

export interface AdminUser {
  id: string;
  username: string;
  created_at: string;
  last_login_at: string | null;
}

export interface SessionResponse {
  kind: "session" | "api_key";
  admin: AdminUser | null;
  /** 会话 Cookie 对应的 CSRF token；API Key 请求为 null。 */
  csrf_token: string | null;
  api_key: {
    id: string;
    workspace_id: string;
    name: string;
    prefix: string;
    scopes: string[];
  } | null;
  scopes: string[];
}

export interface LoginResponse {
  csrf_token: string;
  admin: AdminUser;
}

export interface OverviewStats {
  total_memories: number;
  active_memories: number;
  by_type: Record<string, number>;
  by_scope: Record<string, number>;
  by_status: Record<string, number>;
  pending_review: number;
  total_episodes: number;
  total_entities: number;
  created_last_30_days: number;
}

export interface JobRun {
  id: string;
  type: string;
  scope: string | null;
  status: string;
  params: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface JobListResponse {
  items: JobRun[];
  next_cursor: string | null;
}
