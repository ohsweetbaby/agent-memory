import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArchiveRestore,
  Clock,
  ExternalLink,
  Link2,
  Loader2,
  Pin,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { api, getErrorMessage } from "../../lib/api";
import { formatDateTime } from "../../lib/format";
import {
  MAX_CONTENT_CHARS,
  formatImportance,
  fromDateTimeLocal,
  memoryScopeLabel,
  memoryStatusLabel,
  memoryStatusVariant,
  memoryTags,
  memoryTypeLabel,
  sourceQuote,
  toDateTimeLocal,
  validateImportance,
  validateMemoryContent,
} from "../../lib/memory";
import type {
  AuditEntry,
  AuditListResponse,
  MemoryDetail,
  MemoryRecord,
} from "../../lib/types";
import { Alert } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { ConfirmDialog } from "../ui/confirm-dialog";
import { Textarea } from "../ui/textarea";
import { Input } from "../ui/input";

const AUDIT_ACTION_LABELS: Record<string, string> = {
  "memory.created": "创建",
  "memory.updated": "编辑",
  "memory.pinned": "置顶",
  "memory.deleted": "删除",
  "memory.merged": "合并",
  "memory.superseded": "被新版本取代",
  "memory.feedback": "反馈",
};

const FIELD_LABELS: Record<string, string> = {
  content: "正文",
  summary: "摘要",
  importance: "重要性",
  status: "状态",
  version: "版本",
  valid_to: "有效期",
  tags: "标签",
  metadata: "元数据",
  pinned: "置顶标记",
};

const ACTOR_LABELS: Record<string, string> = {
  admin: "管理员",
  api_key: "API Key",
  system: "系统",
  worker: "后台任务",
  anonymous: "匿名",
};

function auditLabel(action: string): string {
  if (AUDIT_ACTION_LABELS[action]) return AUDIT_ACTION_LABELS[action];
  if (action.startsWith("consolidation.")) return "巩固任务";
  return action;
}

function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field;
}

interface MemoryDetailDrawerProps {
  memoryId: string;
  variant?: "drawer" | "page";
  onClose?: () => void;
  onDeleted?: () => void;
  onSelectMemory?: (id: string) => void;
}

function DetailSection({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="border-t border-slate-100 px-5 py-4">
      <h3 className="mb-3 flex items-center gap-1.5 text-sm font-semibold text-slate-900">
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

export function MemoryDetailDrawer({
  memoryId,
  variant = "drawer",
  onClose,
  onDeleted,
  onSelectMemory,
}: MemoryDetailDrawerProps) {
  const queryClient = useQueryClient();
  const detailQuery = useQuery({
    queryKey: ["memory", memoryId],
    queryFn: () => api<MemoryDetail>(`/memories/${memoryId}`),
    retry: false,
  });
  const auditQuery = useQuery({
    queryKey: ["memory-audit", memoryId],
    queryFn: () => api<AuditListResponse>(`/memories/${memoryId}/audit`),
    retry: false,
  });

  const detail = detailQuery.data;
  const [content, setContent] = useState("");
  const [importance, setImportance] = useState("");
  const [validTo, setValidTo] = useState("");
  const [contentError, setContentError] = useState<string | null>(null);
  const [importanceError, setImportanceError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const initializedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!detail || initializedFor.current === detail.id) return;
    initializedFor.current = detail.id;
    setContent(detail.content);
    setImportance(String(detail.importance));
    setValidTo(toDateTimeLocal(detail.valid_to));
    setContentError(null);
    setImportanceError(null);
    setNotice(null);
  }, [detail]);

  useEffect(() => {
    if (variant !== "drawer" || !onClose || confirmDelete) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [variant, onClose, confirmDelete]);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["memories"] });
    void queryClient.invalidateQueries({ queryKey: ["memory", memoryId] });
    void queryClient.invalidateQueries({ queryKey: ["memory-audit", memoryId] });
    void queryClient.invalidateQueries({ queryKey: ["stats"] });
  };

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!detail) throw new Error("详情尚未加载");
      const validation = validateMemoryContent(content);
      if (!validation.ok) {
        setContentError(validation.message);
        throw new Error(validation.message);
      }
      setContentError(null);

      let parsedImportance: number | null = null;
      if (importance.trim() !== "") {
        parsedImportance = validateImportance(importance);
        if (parsedImportance === null) {
          const message = "重要性必须是 0 到 1 之间的数字";
          setImportanceError(message);
          throw new Error(message);
        }
      }
      setImportanceError(null);

      const payload: Record<string, unknown> = {};
      if (validation.value !== detail.content) payload.content = validation.value;
      if (
        parsedImportance !== null &&
        Math.abs(parsedImportance - detail.importance) > 1e-9
      ) {
        payload.importance = parsedImportance;
      }
      if (validTo !== toDateTimeLocal(detail.valid_to)) {
        payload.valid_to = fromDateTimeLocal(validTo);
      }
      if (Object.keys(payload).length === 0) {
        return { unchanged: true as const };
      }
      const updated = await api<MemoryRecord>(`/memories/${memoryId}`, {
        method: "PATCH",
        json: payload,
      });
      return { unchanged: false as const, updated };
    },
    onSuccess: (result) => {
      if (result.unchanged) {
        setNotice("没有需要保存的修改");
        return;
      }
      setContent(result.updated.content);
      setImportance(String(result.updated.importance));
      setValidTo(toDateTimeLocal(result.updated.valid_to));
      setNotice("已保存");
      refresh();
    },
  });

  const pinMutation = useMutation({
    mutationFn: () => api<MemoryRecord>(`/memories/${memoryId}/pin`, { method: "POST" }),
    onSuccess: (updated) => {
      setImportance(String(updated.importance));
      setNotice("已置顶，重要性提升为 1.0");
      refresh();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api<void>(`/memories/${memoryId}`, { method: "DELETE" }),
    onSuccess: () => {
      setConfirmDelete(false);
      queryClient.removeQueries({ queryKey: ["memory", memoryId] });
      queryClient.removeQueries({ queryKey: ["memory-audit", memoryId] });
      refresh();
      onDeleted?.();
      onClose?.();
    },
  });

  const actionError =
    saveMutation.error ?? pinMutation.error ?? deleteMutation.error;
  const busy =
    saveMutation.isPending || pinMutation.isPending || deleteMutation.isPending;
  const isStale = detail !== undefined && detail.id !== memoryId;

  const panel = (
    <div
      className={
        variant === "drawer"
          ? "flex h-full w-full max-w-2xl flex-col overflow-y-auto bg-white shadow-2xl"
          : "flex w-full flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"
      }
      data-testid="memory-detail"
      role={variant === "drawer" ? "dialog" : undefined}
      aria-modal={variant === "drawer" ? true : undefined}
      aria-label="记忆详情"
    >
      <header className="sticky top-0 z-10 flex items-start justify-between gap-3 border-b border-slate-100 bg-white px-5 py-4">
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-wide text-slate-400">
            记忆详情
          </p>
          <h2 className="mt-0.5 truncate text-lg font-semibold text-slate-900">
            {detail ? detail.content.slice(0, 80) : "正在加载…"}
          </h2>
          {detail ? (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Badge variant={memoryStatusVariant(detail.status)}>
                {memoryStatusLabel(detail.status)}
              </Badge>
              <Badge variant="secondary">{memoryTypeLabel(detail.type)}</Badge>
              <Badge variant="outline">
                {memoryScopeLabel(detail.scope)}
                {detail.scope_id ? ` · ${detail.scope_id}` : ""}
              </Badge>
              {detail.metadata?.pinned === true ? (
                <Badge variant="warning">
                  <Pin className="mr-1 h-3 w-3" aria-hidden="true" />
                  已置顶
                </Badge>
              ) : null}
            </div>
          ) : null}
        </div>
        {onClose ? (
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            aria-label="关闭详情"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </Button>
        ) : null}
      </header>

      {detailQuery.isPending || isStale ? (
        <div className="flex items-center gap-2 px-5 py-10 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          正在加载记忆详情…
        </div>
      ) : null}

      {detailQuery.isError ? (
        <div className="space-y-3 px-5 py-6">
          <Alert>
            详情加载失败：{getErrorMessage(detailQuery.error)}
          </Alert>
          <Button
            variant="outline"
            onClick={() => void detailQuery.refetch()}
            disabled={detailQuery.isFetching}
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            重新加载
          </Button>
        </div>
      ) : null}

      {detail && !isStale ? (
        <>
          {notice ? (
            <div
              className="mx-5 mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700"
              role="status"
            >
              {notice}
            </div>
          ) : null}
          {actionError && !contentError && !importanceError ? (
            <div className="mx-5 mt-4">
              <Alert>操作失败：{getErrorMessage(actionError)}</Alert>
            </div>
          ) : null}

          <DetailSection title="正文">
            <p className="whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-3 text-sm leading-6 text-slate-800">
              {detail.content}
            </p>
            {sourceQuote(detail) ? (
              <blockquote className="mt-3 border-l-2 border-indigo-300 bg-indigo-50/50 px-3 py-2 text-sm text-slate-700">
                <p className="text-xs font-medium text-indigo-700">来源原话</p>
                <p className="mt-1 whitespace-pre-wrap break-words">
                  {sourceQuote(detail)}
                </p>
              </blockquote>
            ) : (
              <p className="mt-2 text-xs text-slate-400">无来源原话</p>
            )}
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs text-slate-500 sm:grid-cols-4">
              <div>
                <dt>来源类型</dt>
                <dd className="mt-0.5 text-slate-700">{detail.source_type}</dd>
              </div>
              <div>
                <dt>来源 ID</dt>
                <dd className="mt-0.5 truncate text-slate-700">
                  {detail.source_id ?? "—"}
                </dd>
              </div>
              <div>
                <dt>版本</dt>
                <dd className="mt-0.5 text-slate-700">
                  v{detail.version} · 访问 {detail.access_count} 次
                </dd>
              </div>
              <div>
                <dt>置信度</dt>
                <dd className="mt-0.5 text-slate-700">
                  {formatImportance(detail.confidence)}
                </dd>
              </div>
            </dl>
            {memoryTags(detail).length > 0 ? (
              <div className="mt-3 flex flex-wrap gap-1">
                {memoryTags(detail).map((tag) => (
                  <Badge key={tag} variant="secondary">
                    {tag}
                  </Badge>
                ))}
              </div>
            ) : null}
          </DetailSection>

          <DetailSection title="编辑">
            <div className="space-y-3">
              <label className="block" htmlFor="memory-edit-content">
                <span className="mb-1 block text-xs font-medium text-slate-600">
                  正文（最多 {MAX_CONTENT_CHARS} 个字符）
                </span>
                <Textarea
                  id="memory-edit-content"
                  value={content}
                  onChange={(event) => {
                    setContent(event.target.value);
                    setContentError(null);
                  }}
                  rows={6}
                  aria-invalid={contentError !== null}
                  aria-describedby={
                    contentError ? "memory-content-error" : undefined
                  }
                />
              </label>
              {contentError ? (
                <p
                  id="memory-content-error"
                  role="alert"
                  className="text-xs text-red-600"
                >
                  {contentError}
                </p>
              ) : null}
              <p className="text-right text-xs text-slate-400">
                {Array.from(content).length} / {MAX_CONTENT_CHARS}
              </p>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <label className="block" htmlFor="memory-edit-importance">
                  <span className="mb-1 block text-xs font-medium text-slate-600">
                    重要性
                  </span>
                  <Input
                    id="memory-edit-importance"
                    type="number"
                    min={0}
                    max={1}
                    step={0.01}
                    value={importance}
                    onChange={(event) => {
                      setImportance(event.target.value);
                      setImportanceError(null);
                    }}
                    aria-invalid={importanceError !== null}
                  />
                  {importanceError ? (
                    <span role="alert" className="mt-1 block text-xs text-red-600">
                      {importanceError}
                    </span>
                  ) : null}
                </label>
                <label className="block" htmlFor="memory-edit-valid-to">
                  <span className="mb-1 block text-xs font-medium text-slate-600">
                    有效期至
                  </span>
                  <Input
                    id="memory-edit-valid-to"
                    type="datetime-local"
                    value={validTo}
                    onChange={(event) => setValidTo(event.target.value)}
                  />
                  <span className="mt-1 block text-xs text-slate-400">
                    留空表示长期有效
                  </span>
                </label>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  onClick={() => saveMutation.mutate()}
                  disabled={busy}
                >
                  {saveMutation.isPending ? "正在保存…" : "保存"}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    pinMutation.reset();
                    pinMutation.mutate();
                  }}
                  disabled={busy || detail.metadata?.pinned === true}
                >
                  <Pin className="h-4 w-4" aria-hidden="true" />
                  {detail.metadata?.pinned === true ? "已置顶" : "置顶"}
                </Button>
                <Button
                  variant="destructive"
                  onClick={() => setConfirmDelete(true)}
                  disabled={busy}
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                  删除
                </Button>
              </div>
              <p className="text-xs text-slate-400">
                删除为软删除，记忆会从检索结果中移除，但保留审计记录。
              </p>
            </div>
          </DetailSection>

          <DetailSection title="有效期与时间" icon={<Clock className="h-4 w-4 text-slate-400" aria-hidden="true" />}>
            <dl className="grid grid-cols-2 gap-3 text-xs text-slate-500 sm:grid-cols-4">
              <div>
                <dt>创建时间</dt>
                <dd className="mt-0.5 text-slate-700">
                  {formatDateTime(detail.created_at)}
                </dd>
              </div>
              <div>
                <dt>更新时间</dt>
                <dd className="mt-0.5 text-slate-700">
                  {formatDateTime(detail.updated_at)}
                </dd>
              </div>
              <div>
                <dt>生效时间</dt>
                <dd className="mt-0.5 text-slate-700">
                  {formatDateTime(detail.valid_from)}
                </dd>
              </div>
              <div>
                <dt>失效时间</dt>
                <dd className="mt-0.5 text-slate-700">
                  {detail.valid_to ? formatDateTime(detail.valid_to) : "长期有效"}
                </dd>
              </div>
            </dl>
          </DetailSection>

          <DetailSection title="关联" icon={<Link2 className="h-4 w-4 text-slate-400" aria-hidden="true" />}>
            {detail.links.length === 0 ? (
              <p className="text-xs text-slate-400">暂无关联记忆。</p>
            ) : (
              <ul className="space-y-2">
                {detail.links.map((link) => {
                  const otherId =
                    link.from_id === detail.id ? link.to_id : link.from_id;
                  const direction =
                    link.from_id === detail.id ? "指向" : "来自";
                  return (
                    <li
                      key={link.id}
                      className="flex items-center justify-between gap-2 rounded-md bg-slate-50 px-3 py-2 text-sm"
                    >
                      <span className="min-w-0 truncate text-slate-700">
                        <Badge variant="secondary">{link.relation}</Badge>
                        <span className="ml-2 text-xs text-slate-500">
                          {direction} {otherId}
                        </span>
                      </span>
                      {onSelectMemory ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => onSelectMemory(otherId)}
                        >
                          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                          查看
                        </Button>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </DetailSection>

          <DetailSection title={`版本链（${detail.history.length}）`} icon={<ArchiveRestore className="h-4 w-4 text-slate-400" aria-hidden="true" />}>
            {detail.history.length === 0 ? (
              <p className="text-xs text-slate-400">当前为唯一版本。</p>
            ) : (
              <ol className="space-y-2">
                {detail.history.map((item) => (
                  <li
                    key={item.id}
                    className="rounded-md border border-slate-100 bg-slate-50 px-3 py-2 text-sm"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs text-slate-500">
                        v{item.version} · {formatDateTime(item.created_at)}
                      </span>
                      <Badge variant={memoryStatusVariant(item.status)}>
                        {memoryStatusLabel(item.status)}
                      </Badge>
                    </div>
                    <p className="mt-1 whitespace-pre-wrap break-words text-slate-700">
                      {item.content}
                    </p>
                  </li>
                ))}
              </ol>
            )}
          </DetailSection>

          <DetailSection title="审计" icon={<Clock className="h-4 w-4 text-slate-400" aria-hidden="true" />}>
            {auditQuery.isPending ? (
              <p className="flex items-center gap-2 text-xs text-slate-500">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                正在读取审计记录…
              </p>
            ) : null}
            {auditQuery.isError ? (
              <Alert variant="info">
                审计记录暂时不可用：{getErrorMessage(auditQuery.error)}
              </Alert>
            ) : null}
            {auditQuery.data ? (
              auditQuery.data.items.length === 0 ? (
                <p className="text-xs text-slate-400">暂无审计记录。</p>
              ) : (
                <ul className="divide-y divide-slate-100" data-testid="memory-audit">
                  {auditQuery.data.items.map((entry: AuditEntry) => (
                    <li
                      key={entry.id}
                      className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
                    >
                      <div className="min-w-0">
                        <p className="text-slate-800">
                          {auditLabel(entry.action)}
                          {entry.changed_fields.length > 0 ? (
                            <span className="ml-1 text-xs text-slate-500">
                              （{entry.changed_fields.map(fieldLabel).join("、")}）
                            </span>
                          ) : null}
                        </p>
                        <p className="text-xs text-slate-400">
                          {ACTOR_LABELS[entry.actor_type] ?? entry.actor_type} ·{" "}
                          {formatDateTime(entry.created_at)}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              )
            ) : null}
          </DetailSection>

          <div className="border-t border-slate-100 px-5 py-3 text-xs text-slate-400">
            记忆 ID：{detail.id} · 工作区：{detail.workspace_id}
          </div>
        </>
      ) : null}

      <ConfirmDialog
        open={confirmDelete}
        title="确认删除这条记忆？"
        description={
          <>
            将软删除「{detail?.content.slice(0, 60)}」。删除后记忆不再出现在列表与检索结果中，
            此操作不会在当前页面物理清除数据。
            {deleteMutation.isError ? (
              <p className="mt-2 text-red-600" role="alert">
                删除失败：{getErrorMessage(deleteMutation.error)}
              </p>
            ) : null}
          </>
        }
        confirmLabel="确认删除"
        destructive
        busy={deleteMutation.isPending}
        onConfirm={() => deleteMutation.mutate()}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );

  if (variant === "page") {
    return panel;
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button
        type="button"
        aria-label="关闭详情遮罩"
        className="absolute inset-0 bg-slate-900/30"
        onClick={onClose}
      />
      <div className="relative z-10 flex h-full w-full justify-end">
        {panel}
      </div>
    </div>
  );
}
