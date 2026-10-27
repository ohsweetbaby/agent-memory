import { describe, expect, it } from "vitest";

import { formatDateTime } from "./format";

describe("formatDateTime", () => {
  it("按本地时区输出固定格式", () => {
    const local = new Date(2026, 9, 27, 21, 5, 9);
    expect(formatDateTime(local)).toBe("2026-10-27 21:05:09");
    expect(formatDateTime(local.toISOString())).toBe("2026-10-27 21:05:09");
  });

  it("补零并处理单数字日期", () => {
    const local = new Date(2026, 0, 2, 3, 4, 5);
    expect(formatDateTime(local)).toBe("2026-01-02 03:04:05");
  });

  it("空值与非法时间返回占位符", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime("")).toBe("—");
    expect(formatDateTime("not-a-date")).toBe("—");
  });
});
