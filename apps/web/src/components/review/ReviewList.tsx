import { Check, ChevronDown, Loader2, Quote, X } from "lucide-react";
import { useState } from "react";

import { formatDateTime } from "../../lib/format";
import {
  formatImportance,
  memoryScopeLabel,
  memoryTypeLabel,
  validateImportance,
  validateMemoryContent,
} from "../../lib/memory";
import type { ReviewItem } from "../../lib/types";
import { Alert } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Textarea } from "../ui/textarea";

interface ReviewListProps {
  items: ReviewItem[];
  selected: Set<string>;
  busyIds: Set<string>;
  errors: Record<string, string>;
  onToggle: (id: string) => void;
  onApprove: (
    item: ReviewItem,
    overrides: { content: string; importance?: number },
  ) => void;
  onReject: (item: ReviewItem, reason: string) => void;
}

function evidenceQuote(item: ReviewItem): string | null {
  const quote = item.evidence?.quote;
  return typeof quote === "string" && quote.trim() !== "" ? quote : null;
}

function ReviewItemCard({
  item,
  selected,
  busy,
  error,
  onToggle,
  onApprove,
  onReject,
}: {
  item: ReviewItem;
  selected: boolean;
  busy: boolean;
  error: string | null;
  onToggle: (id: string) => void;
  onApprove: ReviewListProps["onApprove"];
  onReject: ReviewListProps["onReject"];
}) {
  const [content, setContent] = useState(item.content ?? "");
  const [importance, setImportance] = useState(
    item.importance === null || item.importance === undefined
      ? ""
      : String(item.importance),
  );
  const [localError, setLocalError] = useState<string | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");
  const pending = item.status === "pending";

  const approve = () => {
    const validation = validateMemoryContent(content);
    if (!validation.ok) {
      setLocalError(validation.message);
      return;
    }
    let parsedImportance: number | undefined;
    if (importance.trim() !== "") {
      const parsed = validateImportance(importance);
      if (parsed === null) {
        setLocalError("重要性必须是 0 到 1 之间的数字");
        return;
      }
      parsedImportance = parsed;
    }
    setLocalError(null);
    onApprove(item, {
      content: validation.value,
      importance: parsedImportance,
    });
  };

  return (
    <li
      className={`rounded-xl border bg-white shadow-sm ${
        selected ? "border-indigo-300 ring-1 ring-indigo-100" : "border-slate-200"
      }`}
      data-testid="review-item"
    >
      <div className="flex flex-wrap items-start gap-3 border-b border-slate-100 px-4 py-3">
        {pending ? (
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
            checked={selected}
            onChange={() => onToggle(item.id)}
            aria-label="选择待审项"
          />
        ) : (
          <span className="mt-1 h-4 w-4" aria-hidden="true" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant={pending ? "warning" : item.status === "approved" ? "success" : "secondary"}>
              {pending ? "待审核" : item.status === "approved" ? "已通过" : "已拒绝"}
            </Badge>
            <Badge variant="secondary">{memoryTypeLabel(item.type ?? "")}</Badge>
            <Badge variant="outline">
              {memoryScopeLabel(item.scope ?? "")}
              {item.scope_id ? ` · ${item.scope_id}` : ""}
            </Badge>
            <span className="text-xs text-slate-400">
              置信度 {formatImportance(item.confidence)} ·{" "}
              {formatDateTime(item.created_at)}
            </span>
          </div>
          <p className="mt-1 break-all text-xs text-slate-400">
            {item.id} · 工作区 {item.workspace_id}
          </p>
        </div>
      </div>

      <div className="space-y-3 px-4 py-3">
        {error ? <Alert>操作失败：{error}</Alert> : null}
        {localError ? <Alert>{localError}</Alert> : null}

        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">
            候选正文（通过前可编辑）
          </span>
          <Textarea
            value={content}
            onChange={(event) => {
              setContent(event.target.value);
              setLocalError(null);
            }}
            rows={3}
            disabled={!pending || busy}
            aria-label="候选正文"
          />
        </label>

        <div className="flex flex-wrap items-end gap-3">
          <label className="w-32">
            <span className="mb-1 block text-xs font-medium text-slate-600">
              重要性
            </span>
            <Input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={importance}
              onChange={(event) => {
                setImportance(event.target.value);
                setLocalError(null);
              }}
              disabled={!pending || busy}
              aria-label="重要性"
            />
          </label>

          {evidenceQuote(item) ? (
            <div className="min-w-[220px] flex-1 rounded-md border border-slate-100 bg-slate-50 px-3 py-2">
              <p className="flex items-center gap-1 text-xs font-medium text-slate-500">
                <Quote className="h-3 w-3" aria-hidden="true" />
                来源原话
              </p>
              <p className="mt-1 break-words text-sm text-slate-700">
                {evidenceQuote(item)}
              </p>
            </div>
          ) : null}
        </div>

        {item.entities.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1 text-xs text-slate-500">
            实体：
            {item.entities.map((entity, index) => (
              <Badge key={`${entity.name ?? "entity"}-${index}`} variant="secondary">
                {entity.name ?? "未命名"}
                {entity.type ? ` · ${entity.type}` : ""}
              </Badge>
            ))}
          </div>
        ) : null}

        {item.reviewed_at ? (
          <p className="text-xs text-slate-400">
            审核时间：{formatDateTime(item.reviewed_at)}
          </p>
        ) : null}

        {pending ? (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                onClick={approve}
                disabled={busy}
                className="bg-emerald-600 hover:bg-emerald-700"
              >
                {busy ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                ) : (
                  <Check className="h-4 w-4" aria-hidden="true" />
                )}
                通过
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setRejectOpen((open) => !open)}
                disabled={busy}
              >
                <X className="h-4 w-4" aria-hidden="true" />
                拒绝
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              </Button>
            </div>

            {rejectOpen ? (
              <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-slate-600">
                    拒绝原因（可选）
                  </span>
                  <Input
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="例如：置信度偏低或信息已过期"
                    aria-label="拒绝原因"
                    maxLength={2000}
                  />
                </label>
                <div className="mt-2 flex gap-2">
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => onReject(item, reason.trim())}
                    disabled={busy}
                  >
                    确认拒绝
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setRejectOpen(false)}
                    disabled={busy}
                  >
                    取消
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

export function ReviewList({
  items,
  selected,
  busyIds,
  errors,
  onToggle,
  onApprove,
  onReject,
}: ReviewListProps) {
  return (
    <ul className="space-y-3">
      {items.map((item) => (
        <ReviewItemCard
          key={item.id}
          item={item}
          selected={selected.has(item.id)}
          busy={busyIds.has(item.id)}
          error={errors[item.id] ?? null}
          onToggle={onToggle}
          onApprove={onApprove}
          onReject={onReject}
        />
      ))}
    </ul>
  );
}
