import { ArrowRight, Sparkles } from "lucide-react";

import type { ConsolidationAction, ConsolidationDiff } from "../../lib/types";
import { Badge } from "../ui/badge";

const ACTION_LABELS: Record<string, string> = {
  create: "将新增",
  merge: "将合并",
  supersede: "将取代",
  archive: "将归档",
  episode: "将生成情景",
  nightly_mark: "将标记夜间来源",
};

function actionLabel(action: ConsolidationAction): string {
  return ACTION_LABELS[action.action ?? ""] ?? action.action ?? "变更";
}

function actionSummary(action: ConsolidationAction): string {
  if (action.content) return action.content;
  if (action.summary) return action.summary;
  if (action.target_id) return action.target_id;
  return "";
}

interface DryRunDiffProps {
  diff: ConsolidationDiff;
}

/** dry-run 结果面板：先给总量，再列出每条计划动作。 */
export function DryRunDiff({ diff }: DryRunDiffProps) {
  const totals = [
    { label: "将新增", value: diff.created_memories, suffix: "条记忆" },
    { label: "将生成", value: diff.created_episodes, suffix: "条情景" },
    { label: "将合并", value: diff.merged_memories, suffix: "条" },
    { label: "将取代", value: diff.superseded_memories, suffix: "条" },
    { label: "将归档", value: diff.archived_working, suffix: "条工作记忆" },
  ];

  return (
    <section
      className="rounded-xl border border-indigo-200 bg-indigo-50/50 shadow-sm"
      data-testid="dry-run-diff"
    >
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-indigo-100 px-5 py-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-indigo-600" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-slate-900">
            巩固 dry-run 差异
          </h2>
          <Badge variant="secondary">dry-run · 不会写入</Badge>
        </div>
        <p className="text-xs text-slate-500">
          {diff.job_type === "consolidate_nightly" ? "夜间巩固" : "会话巩固"}
          {diff.replayed ? " · 幂等重放" : ""}
        </p>
      </header>

      <div className="space-y-4 px-5 py-4">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
          {totals.map((item) => (
            <div
              key={item.label}
              className="rounded-lg border border-indigo-100 bg-white px-3 py-2"
            >
              <p className="text-xs text-slate-500">{item.label}</p>
              <p className="mt-0.5 text-lg font-semibold text-slate-900">
                {item.value}
                <span className="ml-1 text-xs font-normal text-slate-400">
                  {item.suffix}
                </span>
              </p>
            </div>
          ))}
        </div>

        {diff.clusters > 0 ? (
          <p className="text-xs text-slate-500">
            共识别 {diff.clusters} 个候选簇；正式运行后仍可能因冲突判定跳过部分动作。
          </p>
        ) : null}

        {diff.actions.length > 0 ? (
          <ul className="space-y-2">
            {diff.actions.slice(0, 30).map((action, index) => (
              <li
                key={`${action.action ?? "action"}-${action.target_id ?? index}`}
                className="flex items-start gap-2 rounded-md border border-slate-200 bg-white px-3 py-2 text-sm"
              >
                <Badge variant="outline">{actionLabel(action)}</Badge>
                <span className="min-w-0 flex-1 break-words text-slate-700">
                  {actionSummary(action) || "—"}
                </span>
                <ArrowRight
                  className="mt-0.5 h-4 w-4 shrink-0 text-slate-300"
                  aria-hidden="true"
                />
              </li>
            ))}
          </ul>
        ) : (
          <p className="rounded-md border border-dashed border-slate-300 bg-white px-3 py-4 text-sm text-slate-500">
            没有可执行的变更；当前窗口内没有达到阈值的候选。
          </p>
        )}
      </div>
    </section>
  );
}
