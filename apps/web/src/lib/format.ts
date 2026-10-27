/** 统一本地时区的时间格式化；后端时间一律为 UTC ISO 字符串。 */
export function formatDateTime(value: string | Date | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  const pad = (part: number): string => String(part).padStart(2, "0");
  return [
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`,
  ].join(" ");
}
