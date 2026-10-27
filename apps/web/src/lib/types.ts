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

export interface WorkspaceRecord {
  id: string;
  name: string;
  created_at: string;
}

export interface WorkspaceListResponse {
  items: WorkspaceRecord[];
  next_cursor: string | null;
}

export interface OpsPolicy {
  decay_archive_threshold: number;
  purge_retention_days: number;
  review_retention_days: number;
  audit_retention_days: number;
  consolidation_idle_minutes: number;
  consolidation_nightly: string;
  search_context_budget_tokens: number;
  search_query_rewrite: boolean;
}

export interface ProviderConfig {
  id: string;
  kind: string;
  name: string;
  base_url: string | null;
  model: string | null;
  dim: number | null;
  extra: Record<string, unknown>;
  enabled: boolean;
  has_secret: boolean;
}

export interface ProviderListResponse {
  items: ProviderConfig[];
  policy: OpsPolicy;
}

export interface ProviderTestResult {
  ok: boolean;
  message: string;
  latency_ms: number;
  status_code: number | null;
}

export interface ApiKeyRecord {
  id: string;
  workspace_id: string;
  name: string;
  prefix: string;
  scopes: string[];
  rate_limit: number | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ApiKeyListResponse {
  items: ApiKeyRecord[];
  next_cursor: string | null;
}

export interface ApiKeyCreateResponse extends ApiKeyRecord {
  /** 完整明文仅在创建响应中出现一次。 */
  key: string;
}

export interface ConsolidationAction {
  action?: string;
  target_id?: string | null;
  content?: string | null;
  reason?: string | null;
  status?: string;
  summary?: string | null;
}

export interface ConsolidationDiff {
  job_run_id: string | null;
  job_run_ids: string[];
  job_type: string;
  dry_run: boolean;
  replayed: boolean;
  created_episodes: number;
  created_memories: number;
  merged_memories: number;
  superseded_memories: number;
  archived_working: number;
  clusters: number;
  actions: ConsolidationAction[];
  skipped_actions: ConsolidationAction[];
}

export interface SearchWhy {
  vector_rank: number | null;
  bm25_rank: number | null;
  graph_rank: number | null;
  prior_rank: number | null;
  rrf: number;
}

export interface SearchItem {
  id: string;
  type: string;
  scope: string;
  scope_id: string | null;
  content: string;
  importance: number;
  confidence: number;
  retention: number;
  status: string;
  historical: boolean;
  valid_from: string;
  timestamp: string;
  superseded_by: string | null;
  why: SearchWhy;
  score: number;
}

export interface SearchStats {
  candidates: number;
  returned: number;
  latency_ms: number;
  mode: string;
  channels: Record<string, number>;
}

export interface SearchResponse {
  items: SearchItem[];
  context_text: string;
  stats: SearchStats;
  degraded: string[];
}
