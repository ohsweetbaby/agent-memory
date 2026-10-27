import { Brain, ChevronRight } from "lucide-react";

import { formatDateTime } from "../../lib/format";
import {
  formatImportance,
  memoryScopeLabel,
  memoryStatusLabel,
  memoryStatusVariant,
  memoryTypeLabel,
} from "../../lib/memory";
import type { MemoryRecord } from "../../lib/types";
import { Badge } from "../ui/badge";

interface MemoryTableProps {
  items: MemoryRecord[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function MemoryTable({ items, selectedId, onSelect }: MemoryTableProps) {
  if (items.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-300 bg-white py-16 text-sm text-slate-500"
        data-testid="memory-empty"
      >
        <Brain className="h-8 w-8 text-slate-300" aria-hidden="true" />
        <p>没有符合条件的记忆</p>
        <p className="text-xs text-slate-400">
          调整关键词或筛选条件后再试。
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] table-fixed text-left text-sm">
          <thead className="border-b border-slate-100 bg-slate-50 text-xs text-slate-500">
            <tr>
              <th className="w-[46%] px-4 py-2 font-medium">正文</th>
              <th className="w-[12%] px-3 py-2 font-medium">类型</th>
              <th className="w-[12%] px-3 py-2 font-medium">作用域</th>
              <th className="w-[10%] px-3 py-2 font-medium">重要性</th>
              <th className="w-[10%] px-3 py-2 font-medium">状态</th>
              <th className="w-[10%] px-3 py-2 font-medium">更新时间</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {items.map((memory) => (
              <tr
                key={memory.id}
                data-testid="memory-row"
                className={
                  selectedId === memory.id
                    ? "bg-indigo-50/70"
                    : "hover:bg-slate-50"
                }
              >
                <td className="px-4 py-3 align-top">
                  <button
                    type="button"
                    data-testid="memory-row-content"
                    onClick={() => onSelect(memory.id)}
                    className="group flex w-full items-start gap-1 text-left"
                    aria-label={`查看记忆：${memory.content.slice(0, 60)}`}
                  >
                    <span className="line-clamp-3 min-w-0 flex-1 whitespace-pre-wrap break-words text-slate-800 group-hover:text-indigo-700">
                      {memory.content}
                    </span>
                    <ChevronRight
                      className="mt-0.5 h-4 w-4 shrink-0 text-slate-300 group-hover:text-indigo-500"
                      aria-hidden="true"
                    />
                  </button>
                  <p className="mt-1 truncate text-xs text-slate-400">
                    {memory.id}
                  </p>
                </td>
                <td className="px-3 py-3 align-top text-slate-600">
                  {memoryTypeLabel(memory.type)}
                </td>
                <td className="px-3 py-3 align-top text-slate-600">
                  {memoryScopeLabel(memory.scope)}
                  {memory.scope_id ? (
                    <span className="block truncate text-xs text-slate-400">
                      {memory.scope_id}
                    </span>
                  ) : null}
                </td>
                <td className="px-3 py-3 align-top font-medium text-slate-700">
                  {formatImportance(memory.importance)}
                </td>
                <td className="px-3 py-3 align-top">
                  <Badge variant={memoryStatusVariant(memory.status)}>
                    {memoryStatusLabel(memory.status)}
                  </Badge>
                </td>
                <td className="px-3 py-3 align-top text-xs text-slate-500">
                  {formatDateTime(memory.updated_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
