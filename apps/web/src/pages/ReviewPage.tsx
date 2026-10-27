import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ClipboardCheck,
  Loader2,
  RefreshCw,
  ShieldCheck,
  ShieldX,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { ReviewList } from "../components/review/ReviewList";
import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Textarea } from "../components/ui/textarea";
import { api, getErrorMessage } from "../lib/api";
import type {
  BulkReviewResult,
  ReviewActionResult,
  ReviewItem,
  ReviewListResponse,
} from "../lib/types";

const PAGE_SIZE = 20;

function buildQuery(status: string, cursor: string | null): string {
  const params = new URLSearchParams({
    status,
    limit: String(PAGE_SIZE),
  });
  if (cursor) params.set("cursor", cursor);
  return params.toString();
}

export function ReviewPage() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState("pending");
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [banner, setBanner] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkRejectOpen, setBulkRejectOpen] = useState(false);
  const [bulkReason, setBulkReason] = useState("");

  const cursor = cursors[pageIndex] ?? null;
  const query = useMemo(() => buildQuery(status, cursor), [status, cursor]);

  const listQuery = useQuery({
    queryKey: ["review", "list", query],
    queryFn: () => api<ReviewListResponse>(`/review?${query}`),
    retry: false,
  });

  const items = listQuery.data?.items ?? [];
  const pendingItems = items.filter((item) => item.status === "pending");
  const selectedItems = pendingItems.filter((item) => selected.has(item.id));

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["review"] });
    void queryClient.invalidateQueries({ queryKey: ["stats"] });
    void queryClient.invalidateQueries({ queryKey: ["memories"] });
  }, [queryClient]);

  const setItemBusy = (id: string, busy: boolean) => {
    setBusyIds((current) => {
      const next = new Set(current);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const clearError = (id: string) => {
    setErrors((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  };

  const approveItem = async (
    item: ReviewItem,
    overrides: { content: string; importance?: number },
  ) => {
    clearError(item.id);
    setItemBusy(item.id, true);
    try {
      await api<ReviewActionResult>(`/review/${item.id}/approve`, {
        method: "POST",
        json: {
          content: overrides.content,
          ...(overrides.importance !== undefined
            ? { importance: overrides.importance }
            : {}),
        },
      });
      setBanner({ kind: "success", text: "已通过并生成记忆" });
      setSelected((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
      refresh();
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [item.id]: getErrorMessage(error),
      }));
    } finally {
      setItemBusy(item.id, false);
    }
  };

  const rejectItem = async (item: ReviewItem, reason: string) => {
    clearError(item.id);
    setItemBusy(item.id, true);
    try {
      await api<ReviewActionResult>(`/review/${item.id}/reject`, {
        method: "POST",
        json: reason ? { reason } : {},
      });
      setBanner({ kind: "success", text: "已拒绝待审候选" });
      setSelected((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
      refresh();
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [item.id]: getErrorMessage(error),
      }));
    } finally {
      setItemBusy(item.id, false);
    }
  };

  const runBulkFallback = async (
    action: "approve" | "reject",
    reason: string,
  ): Promise<{
    succeeded: number;
    failed: number;
    firstError: string | null;
    failedIds: string[];
  }> => {
    // 批量端点是逐条提交的：中途失败时部分项可能已经处理完成。
    // 先读取最新 pending 集合，避免把“已成功但响应失败”的项误报为失败。
    let stillPending: Set<string> | null = null;
    try {
      const snapshot = await api<ReviewListResponse>(
        "/review?status=pending&limit=100",
      );
      stillPending = new Set(snapshot.items.map((item) => item.id));
    } catch {
      stillPending = null;
    }

    let succeeded = 0;
    const failedIds: string[] = [];
    let firstError: string | null = null;
    for (const item of selectedItems) {
      if (stillPending !== null && !stillPending.has(item.id)) {
        succeeded += 1;
        continue;
      }
      try {
        if (action === "approve") {
          await api<ReviewActionResult>(`/review/${item.id}/approve`, {
            method: "POST",
            json: {},
          });
        } else {
          await api<ReviewActionResult>(`/review/${item.id}/reject`, {
            method: "POST",
            json: reason ? { reason } : {},
          });
        }
        succeeded += 1;
      } catch (error) {
        failedIds.push(item.id);
        const message = getErrorMessage(error);
        firstError ??= message;
        setErrors((current) => ({ ...current, [item.id]: message }));
      }
    }
    return {
      succeeded,
      failed: failedIds.length,
      firstError,
      failedIds,
    };
  };

  const runBulk = async (action: "approve" | "reject", reason = "") => {
    if (selectedItems.length === 0) {
      setBanner({ kind: "error", text: "请先选择待审项" });
      return;
    }
    const ids = selectedItems.map((item) => item.id);
    clearSelectedErrors(ids);
    setBulkBusy(true);
    setBusyIds(new Set(ids));
    try {
      await api<BulkReviewResult>("/review/bulk", {
        method: "POST",
        json: {
          ids,
          action,
          ...(reason ? { reason } : {}),
        },
      });
      setBanner({
        kind: "success",
        text:
          action === "approve"
            ? `已批量通过 ${ids.length} 条待审项`
            : `已批量拒绝 ${ids.length} 条待审项`,
      });
      setSelected(new Set());
      refresh();
    } catch (bulkError) {
      // 批量接口整体失败时按条重试，逐条呈现原因，避免一条坏数据拖垮整批。
      const fallback = await runBulkFallback(action, reason);
      const reasonText = fallback.firstError ?? getErrorMessage(bulkError);
      if (fallback.failed === 0) {
        setBanner({
          kind: "success",
          text: `批量接口失败后逐条重试完成：${fallback.succeeded} 条`,
        });
        setSelected(new Set());
      } else {
        setBanner({
          kind: "error",
          text: `批量操作部分失败：成功 ${fallback.succeeded} 条，失败 ${fallback.failed} 条。原因：${reasonText}`,
        });
        setSelected(new Set(fallback.failedIds));
      }
      refresh();
    } finally {
      setBulkBusy(false);
      setBusyIds(new Set());
      setBulkRejectOpen(false);
    }
  };

  const clearSelectedErrors = (ids: string[]) => {
    setErrors((current) => {
      const next = { ...current };
      for (const id of ids) delete next[id];
      return next;
    });
  };

  const toggle = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (selectedItems.length === pendingItems.length) {
      setSelected(new Set());
      return;
    }
    setSelected(new Set(pendingItems.map((item) => item.id)));
  };

  const changeStatus = (next: string) => {
    setStatus(next);
    setCursors([null]);
    setPageIndex(0);
    setSelected(new Set());
    setErrors({});
    setBanner(null);
  };

  const nextCursor = listQuery.data?.next_cursor ?? null;
  const allSelected =
    pendingItems.length > 0 && selectedItems.length === pendingItems.length;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-slate-900">
            <ClipboardCheck className="h-6 w-6 text-amber-500" aria-hidden="true" />
            待审队列
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            审核抽取管线产生的低置信度候选；通过前可编辑正文与重要性。
          </p>
        </div>
        <label className="text-xs text-slate-500">
          <span className="mr-2">状态</span>
          <select
            className="h-9 rounded-md border border-slate-300 bg-white px-2 text-sm text-slate-800 focus-visible:border-indigo-500 focus-visible:outline-none"
            value={status}
            onChange={(event) => changeStatus(event.target.value)}
            aria-label="待审状态"
          >
            <option value="pending">待审核</option>
            <option value="approved">已通过</option>
            <option value="rejected">已拒绝</option>
            <option value="all">全部</option>
          </select>
        </label>
      </header>

      {banner ? (
        banner.kind === "success" ? (
          <div
            className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700"
            role="status"
          >
            {banner.text}
          </div>
        ) : (
          <Alert>{banner.text}</Alert>
        )
      ) : null}

      {status === "pending" ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
              checked={allSelected}
              onChange={toggleAll}
              disabled={pendingItems.length === 0}
              aria-label="全选待审项"
            />
            已选 {selectedItems.length} / {pendingItems.length}
          </label>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              onClick={() => void runBulk("approve")}
              disabled={selectedItems.length === 0 || bulkBusy}
            >
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
              批量通过
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setBulkReason("");
                setBulkRejectOpen(true);
              }}
              disabled={selectedItems.length === 0 || bulkBusy}
            >
              <ShieldX className="h-4 w-4" aria-hidden="true" />
              批量拒绝
            </Button>
          </div>
        </div>
      ) : null}

      {listQuery.isPending ? (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          正在加载待审队列…
        </div>
      ) : null}

      {listQuery.isError ? (
        <div className="space-y-3 rounded-xl border border-slate-200 bg-white p-5">
          <Alert>待审队列加载失败：{getErrorMessage(listQuery.error)}</Alert>
          <Button
            variant="outline"
            onClick={() => void listQuery.refetch()}
            disabled={listQuery.isFetching}
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            重新加载
          </Button>
        </div>
      ) : null}

      {listQuery.data && items.length === 0 ? (
        <div
          className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-300 bg-white py-16 text-sm text-slate-500"
          data-testid="review-empty"
        >
          <ClipboardCheck className="h-8 w-8 text-slate-300" aria-hidden="true" />
          <p>{status === "pending" ? "待审队列为空" : "没有符合条件的待审项"}</p>
          <p className="text-xs text-slate-400">
            {status === "pending"
              ? "新的低置信度候选会出现在这里。"
              : "切换状态可查看已处理的候选。"}
          </p>
        </div>
      ) : null}

      {items.length > 0 ? (
        <>
          <ReviewList
            items={items}
            selected={selected}
            busyIds={busyIds}
            errors={errors}
            onToggle={toggle}
            onApprove={(item, overrides) => void approveItem(item, overrides)}
            onReject={(item, reason) => void rejectItem(item, reason)}
          />

          <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-slate-500">
            <span>第 {pageIndex + 1} 页</span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex((index) => Math.max(0, index - 1))}
                disabled={pageIndex === 0 || listQuery.isFetching}
              >
                上一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  if (!nextCursor) return;
                  setCursors((stack) => [
                    ...stack.slice(0, pageIndex + 1),
                    nextCursor,
                  ]);
                  setPageIndex((index) => index + 1);
                  setSelected(new Set());
                }}
                disabled={!nextCursor || listQuery.isFetching}
              >
                下一页
              </Button>
            </div>
          </div>
        </>
      ) : null}

      {bulkRejectOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="批量拒绝"
            className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-5 shadow-xl"
          >
            <h2 className="text-base font-semibold text-slate-900">
              批量拒绝 {selectedItems.length} 条待审项
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              原因可选，仅用于审计记录。
            </p>
            <label className="mt-4 block">
              <span className="mb-1 block text-xs font-medium text-slate-600">
                拒绝原因（可选）
              </span>
              <Textarea
                value={bulkReason}
                onChange={(event) => setBulkReason(event.target.value)}
                rows={3}
                maxLength={2000}
                aria-label="批量拒绝原因"
                placeholder="例如：批量识别为低置信度重复候选"
              />
            </label>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => setBulkRejectOpen(false)}
                disabled={bulkBusy}
              >
                取消
              </Button>
              <Button
                variant="destructive"
                onClick={() => void runBulk("reject", bulkReason.trim())}
                disabled={bulkBusy}
              >
                {bulkBusy ? "处理中…" : "确认拒绝"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
