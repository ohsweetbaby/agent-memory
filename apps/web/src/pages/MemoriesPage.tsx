import { useQuery } from "@tanstack/react-query";
import { Brain, ChevronLeft, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import {
  EMPTY_MEMORY_FILTERS,
  MemoryFilters,
  type MemoryFilterState,
} from "../components/memory/MemoryFilters";
import { MemoryDetailDrawer } from "../components/memory/MemoryDetailDrawer";
import { MemoryTable } from "../components/memory/MemoryTable";
import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { api, getErrorMessage } from "../lib/api";
import type { MemoryListResponse } from "../lib/types";

const PAGE_SIZE = 20;

function buildQuery(filters: MemoryFilterState, cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (filters.type) params.set("type", filters.type);
  if (filters.scope) params.set("scope", filters.scope);
  if (filters.status) params.set("status", filters.status);
  if (filters.minImportance) {
    params.set("min_importance", filters.minImportance);
  }
  if (cursor) params.set("cursor", cursor);
  return params.toString();
}

export function MemoriesPage() {
  const [filters, setFilters] = useState<MemoryFilterState>(EMPTY_MEMORY_FILTERS);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const cursor = cursors[pageIndex] ?? null;
  const query = useMemo(() => buildQuery(filters, cursor), [filters, cursor]);

  const listQuery = useQuery({
    queryKey: ["memories", "list", query],
    queryFn: () => api<MemoryListResponse>(`/memories?${query}`),
    retry: false,
  });

  const handleFilterChange = useCallback((next: MemoryFilterState) => {
    setFilters(next);
    setCursors([null]);
    setPageIndex(0);
  }, []);

  const resetFilters = useCallback(() => {
    setFilters(EMPTY_MEMORY_FILTERS);
    setCursors([null]);
    setPageIndex(0);
  }, []);

  const items = listQuery.data?.items ?? [];
  const nextCursor = listQuery.data?.next_cursor ?? null;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-slate-900">
            <Brain className="h-6 w-6 text-indigo-500" aria-hidden="true" />
            记忆浏览器
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            按关键词、类型、作用域与重要性筛选；点击记忆查看详情并编辑。
          </p>
        </div>
        <p className="text-xs text-slate-400">
          {listQuery.data
            ? `本页 ${items.length} 条 · 服务端筛选`
            : "正在读取记忆…"}
        </p>
      </header>

      <MemoryFilters
        value={filters}
        onChange={handleFilterChange}
        onReset={resetFilters}
      />

      {notice ? (
        <div
          className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700"
          role="status"
        >
          {notice}
        </div>
      ) : null}

      {listQuery.isPending ? (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          正在加载记忆…
        </div>
      ) : null}

      {listQuery.isError ? (
        <div className="space-y-3 rounded-xl border border-slate-200 bg-white p-5">
          <Alert>记忆列表加载失败：{getErrorMessage(listQuery.error)}</Alert>
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

      {listQuery.data ? (
        <>
          <MemoryTable
            items={items}
            selectedId={selectedId}
            onSelect={(id) => {
              setNotice(null);
              setSelectedId(id);
            }}
          />
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-slate-500">
            <span>第 {pageIndex + 1} 页</span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setPageIndex((index) => Math.max(0, index - 1));
                  setSelectedId(null);
                }}
                disabled={pageIndex === 0 || listQuery.isFetching}
              >
                <ChevronLeft className="h-4 w-4" aria-hidden="true" />
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
                  setSelectedId(null);
                }}
                disabled={!nextCursor || listQuery.isFetching}
              >
                下一页
                <ChevronRight className="h-4 w-4" aria-hidden="true" />
              </Button>
            </div>
          </div>
        </>
      ) : null}

      {selectedId ? (
        <MemoryDetailDrawer
          memoryId={selectedId}
          onClose={() => setSelectedId(null)}
          onDeleted={() => {
            setSelectedId(null);
            setNotice("已删除记忆，列表已刷新");
            void listQuery.refetch();
          }}
          onSelectMemory={(id) => setSelectedId(id)}
        />
      ) : null}
    </div>
  );
}
