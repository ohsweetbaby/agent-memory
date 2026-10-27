import { AlertTriangle, Info } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../../lib/utils";

interface AlertProps {
  children: ReactNode;
  variant?: "destructive" | "info";
  className?: string;
}

/** 统一的可读错误/提示条；不使用 HTML 注入。 */
export function Alert({ children, variant = "destructive", className }: AlertProps) {
  const Icon = variant === "destructive" ? AlertTriangle : Info;
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-md border px-3 py-2 text-sm",
        variant === "destructive"
          ? "border-red-200 bg-red-50 text-red-700"
          : "border-sky-200 bg-sky-50 text-sky-800",
        className,
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 break-words">{children}</div>
    </div>
  );
}
