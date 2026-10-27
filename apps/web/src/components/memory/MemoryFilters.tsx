import { Search, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useState } from "react";

import {
  MEMORY_SCOPE_OPTIONS,
  MEMORY_STATUS_OPTIONS,
  MEMORY_TYPE_OPTIONS,
} from "../../lib/memory";
import { Button } from "../ui/button";
import { Input } from "../ui/input";

export interface MemoryFilterState {
  q: string;
  type: string;
  scope: string;
  status: string;
  minImportance: string;
}

export const EMPTY_MEMORY_FILTERS: MemoryFilterState = {
  q: "",
  type: "",
  scope: "",
  status: "",
  minImportance: "",
};

interface MemoryFiltersProps {
  value: MemoryFilterState;
  onChange: (next: MemoryFilterState) => void;
  onReset: () => void;
}

const selectClass =
  "h-10 w-full rounded-md border border-slate-300 bg-white px-2 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-200";

export function MemoryFilters({
  value,
  onChange,
  onReset,
}: MemoryFiltersProps) {
  const [query, setQuery] = useState(value.q);

  // 输入防抖，避免每个按键都请求一次列表。
  useEffect(() => {
    if (query === value.q) return;
    const timer = window.setTimeout(() => {
      onChange({ ...value, q: query });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [query, value, onChange]);

  useEffect(() => {
    setQuery(value.q);
  }, [value.q]);

  const update = <K extends keyof MemoryFilterState>(
    key: K,
    next: MemoryFilterState[K],
  ) => {
    onChange({ ...value, [key]: next });
  };

  return (
    <div
      className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
      data-testid="memory-filters"
    >
      <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-[220px] flex-1">
          <span className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-600">
            <Search className="h-3.5 w-3.5" aria-hidden="true" />
            关键词
          </span>
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索记忆"
            aria-label="搜索记忆"
          />
        </label>

        <label className="w-36">
          <span className="mb-1 block text-xs font-medium text-slate-600">
            类型
          </span>
          <select
            className={selectClass}
            aria-label="类型"
            value={value.type}
            onChange={(event) => update("type", event.target.value)}
          >
            <option value="">全部类型</option>
            {MEMORY_TYPE_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>

        <label className="w-32">
          <span className="mb-1 block text-xs font-medium text-slate-600">
            作用域
          </span>
          <select
            className={selectClass}
            aria-label="作用域"
            value={value.scope}
            onChange={(event) => update("scope", event.target.value)}
          >
            <option value="">全部作用域</option>
            {MEMORY_SCOPE_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>

        <label className="w-32">
          <span className="mb-1 block text-xs font-medium text-slate-600">
            状态
          </span>
          <select
            className={selectClass}
            aria-label="状态"
            value={value.status}
            onChange={(event) => update("status", event.target.value)}
          >
            <option value="">默认（活跃）</option>
            {MEMORY_STATUS_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>

        <label className="w-36">
          <span className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-600">
            <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
            最低重要性
          </span>
          <select
            className={selectClass}
            aria-label="最低重要性"
            value={value.minImportance}
            onChange={(event) => update("minImportance", event.target.value)}
          >
            <option value="">不限</option>
            <option value="0.5">≥ 0.50</option>
            <option value="0.7">≥ 0.70</option>
            <option value="0.9">≥ 0.90</option>
          </select>
        </label>

        <Button variant="ghost" onClick={onReset} aria-label="重置筛选">
          <X className="h-4 w-4" aria-hidden="true" />
          重置
        </Button>
      </div>
    </div>
  );
}
