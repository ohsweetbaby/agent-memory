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

export type MemoryType =
  | "working"
  | "episodic"
  | "semantic"
  | "procedural"
  | "perceptual";

export type MemoryScope = "global" | "agent" | "user" | "session";

export type MemoryStatus = "active" | "archived" | "superseded";

export interface MemoryRecord {
  id: string;
  workspace_id: string;
  scope: MemoryScope | string;
  scope_id: string | null;
  type: MemoryType | string;
  content: string;
  summary: string | null;
  importance: number;
  confidence: number;
  stability: number;
  access_count: number;
  version: number;
  status: MemoryStatus | string;
  superseded_by: string | null;
  valid_from: string;
  valid_to: string | null;
  source_type: string;
  source_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface MemoryLink {
  id: string;
  from_id: string;
  to_id: string;
  relation: string;
  weight: number | null;
  evidence: Record<string, unknown> | null;
}

export interface MemoryDetail extends MemoryRecord {
  history: MemoryRecord[];
  links: MemoryLink[];
}

export interface MemoryListResponse {
  items: MemoryRecord[];
  next_cursor: string | null;
}

export interface AuditEntry {
  id: string;
  action: string;
  actor_type: string;
  changed_fields: string[];
  created_at: string;
}

export interface AuditListResponse {
  items: AuditEntry[];
}

export interface ReviewEntity {
  name?: string;
  type?: string | null;
  role?: string | null;
}

export interface ReviewItem {
  id: string;
  workspace_id: string;
  status: "pending" | "approved" | "rejected" | string;
  content: string | null;
  type: string | null;
  importance: number | null;
  confidence: number;
  evidence: Record<string, unknown> | null;
  entities: ReviewEntity[];
  scope: string | null;
  scope_id: string | null;
  candidate: Record<string, unknown>;
  created_at: string;
  reviewed_at: string | null;
}

export interface ReviewListResponse {
  items: ReviewItem[];
  next_cursor: string | null;
}

export interface ReviewActionResult {
  review_item_id: string;
  memory_id?: string;
  status: string;
  action?: string;
  reviewed_at?: string | null;
}

export interface BulkReviewResult {
  approved: number;
  rejected: number;
  memory_ids: string[];
}
