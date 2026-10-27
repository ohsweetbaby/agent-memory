/** 记忆浏览器/待审队列共用的标签、校验与展示工具。 */

import type {
  MemoryRecord,
  MemoryScope,
  MemoryStatus,
  MemoryType,
} from "./types";

export const MAX_CONTENT_CHARS = 8192;

export const MEMORY_TYPE_OPTIONS: ReadonlyArray<{
  value: MemoryType;
  label: string;
}> = [
  { value: "working", label: "工作记忆" },
  { value: "episodic", label: "情景记忆" },
  { value: "semantic", label: "语义记忆" },
  { value: "procedural", label: "程序记忆" },
  { value: "perceptual", label: "感知记忆" },
];

export const MEMORY_SCOPE_OPTIONS: ReadonlyArray<{
  value: MemoryScope;
  label: string;
}> = [
  { value: "global", label: "全局" },
  { value: "agent", label: "智能体" },
  { value: "user", label: "用户" },
  { value: "session", label: "会话" },
];

export const MEMORY_STATUS_OPTIONS: ReadonlyArray<{
  value: MemoryStatus;
  label: string;
}> = [
  { value: "active", label: "活跃" },
  { value: "archived", label: "已归档" },
  { value: "superseded", label: "已取代" },
];

const TYPE_LABELS: Record<string, string> = Object.fromEntries(
  MEMORY_TYPE_OPTIONS.map((item) => [item.value, item.label]),
);
const SCOPE_LABELS: Record<string, string> = Object.fromEntries(
  MEMORY_SCOPE_OPTIONS.map((item) => [item.value, item.label]),
);
const STATUS_LABELS: Record<string, string> = Object.fromEntries(
  MEMORY_STATUS_OPTIONS.map((item) => [item.value, item.label]),
);

export function memoryTypeLabel(value: string): string {
  return TYPE_LABELS[value] ?? value;
}

export function memoryScopeLabel(value: string): string {
  return SCOPE_LABELS[value] ?? value;
}

export function memoryStatusLabel(value: string): string {
  return STATUS_LABELS[value] ?? value;
}

export function memoryStatusVariant(
  value: string,
): "success" | "warning" | "secondary" | "outline" {
  if (value === "active") return "success";
  if (value === "archived") return "warning";
  if (value === "superseded") return "outline";
  return "secondary";
}

export function formatImportance(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : "—";
}

export function sourceQuote(memory: MemoryRecord): string | null {
  const raw = memory.metadata?.source_quote;
  return typeof raw === "string" && raw.trim() !== "" ? raw : null;
}

export function memoryTags(memory: MemoryRecord): string[] {
  const raw = memory.metadata?.tags;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is string => typeof item === "string");
}

export type ContentValidation =
  | { ok: true; value: string }
  | { ok: false; message: string };

/**
 * 正文校验与后端 `normalize_content` 的边界保持一致：去首尾空白后
 * 非空且不超过 8192 字符（8 KB 量级）。
 */
export function validateMemoryContent(raw: string): ContentValidation {
  const value = raw.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (value === "") {
    return { ok: false, message: "正文不能为空" };
  }
  if (value.includes("\u0000")) {
    return { ok: false, message: "正文不能包含空字符（NUL）" };
  }
  if (value.length > MAX_CONTENT_CHARS) {
    return {
      ok: false,
      message: `正文不能超过 8 KB（${MAX_CONTENT_CHARS} 个字符），请精简后重试`,
    };
  }
  return { ok: true, value };
}

export function validateImportance(raw: string): number | null {
  if (raw.trim() === "") return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > 1) return null;
  return value;
}

/** `<input type="datetime-local">` 需要本地时间且不含时区后缀。 */
export function toDateTimeLocal(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 空串表示清除有效期；返回 ISO 字符串或 null。 */
export function fromDateTimeLocal(value: string): string | null {
  if (value.trim() === "") return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}
