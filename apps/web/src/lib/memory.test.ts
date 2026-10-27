import { describe, expect, it } from "vitest";

import {
  MAX_CONTENT_CHARS,
  formatImportance,
  fromDateTimeLocal,
  memoryScopeLabel,
  memoryStatusLabel,
  memoryTypeLabel,
  sourceQuote,
  toDateTimeLocal,
  validateImportance,
  validateMemoryContent,
} from "./memory";
import type { MemoryRecord } from "./types";

function memory(overrides: Partial<MemoryRecord> = {}): MemoryRecord {
  return {
    id: "memory-1",
    workspace_id: "workspace-1",
    scope: "user",
    scope_id: "user-1",
    type: "semantic",
    content: "用户对花生过敏",
    summary: null,
    importance: 0.6,
    confidence: 0.8,
    stability: 1,
    access_count: 0,
    version: 1,
    status: "active",
    superseded_by: null,
    valid_from: "2026-10-01T00:00:00Z",
    valid_to: null,
    source_type: "manual",
    source_id: null,
    metadata: {},
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    deleted_at: null,
    ...overrides,
  };
}

describe("validateMemoryContent", () => {
  it("规范化换行并拒绝空正文", () => {
    expect(validateMemoryContent("  第一行\r\n第二行  ")).toEqual({
      ok: true,
      value: "第一行\n第二行",
    });
    expect(validateMemoryContent("   \n ")).toEqual({
      ok: false,
      message: "正文不能为空",
    });
  });

  it("8192 个字符恰好通过，8193 个字符拒绝", () => {
    expect(validateMemoryContent("记".repeat(MAX_CONTENT_CHARS)).ok).toBe(true);
    const result = validateMemoryContent(
      "记".repeat(MAX_CONTENT_CHARS + 1),
    );
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.message).toContain(`${MAX_CONTENT_CHARS} 个字符`);
      expect(result.message).not.toContain("8 KB");
    }
  });

  it("按 Unicode code point 计数，emoji 不被算成两个字符", () => {
    const emoji = "😀".repeat(MAX_CONTENT_CHARS);
    expect(emoji.length).toBe(MAX_CONTENT_CHARS * 2);
    expect(validateMemoryContent(emoji).ok).toBe(true);
    expect(
      validateMemoryContent(`${emoji}😀`).ok,
    ).toBe(false);
  });

  it("拒绝 NUL 字符", () => {
    const result = validateMemoryContent("正常\u0000文本");
    expect(result.ok).toBe(false);
  });
});

describe("validateImportance", () => {
  it("接受 0..1 并拒绝越界/非数字", () => {
    expect(validateImportance("0.9")).toBe(0.9);
    expect(validateImportance("0")).toBe(0);
    expect(validateImportance("")).toBeNull();
    expect(validateImportance("1.1")).toBeNull();
    expect(validateImportance("abc")).toBeNull();
  });
});

describe("记忆展示辅助", () => {
  it("映射类型、作用域、状态与重要性", () => {
    expect(memoryTypeLabel("semantic")).toBe("语义记忆");
    expect(memoryScopeLabel("session")).toBe("会话");
    expect(memoryStatusLabel("superseded")).toBe("已取代");
    expect(formatImportance(0.9)).toBe("0.90");
  });

  it("只读取字符串来源原话", () => {
    expect(sourceQuote(memory({ metadata: { source_quote: "原话" } }))).toBe(
      "原话",
    );
    expect(sourceQuote(memory({ metadata: { source_quote: 42 } }))).toBeNull();
  });
});

describe("datetime-local 转换", () => {
  it("往返本地时间", () => {
    const value = "2026-10-28T09:30";
    const iso = fromDateTimeLocal(value);
    expect(iso).not.toBeNull();
    expect(toDateTimeLocal(iso)).toBe(value);
  });

  it("空值表示长期有效", () => {
    expect(fromDateTimeLocal("")).toBeNull();
    expect(toDateTimeLocal(null)).toBe("");
  });
});
