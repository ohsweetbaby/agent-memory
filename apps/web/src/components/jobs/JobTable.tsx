import { Loader2, RefreshCw, RotateCcw, Radio } from "lucide-react";

import { formatDateTime } from "../../lib/format";
import type { JobRun } from "../../lib/types";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";

const JOB_TYPE_LABELS: Record<string, string> = {
  consolidate_session: "会话巩固",
  consolidate_nightly: "夜间巩固",
  export: "导出",
  decay_sweep: "遗忘扫描",
  purge_deleted: "清理已删除",
};

const STATUS_META: Record<
  string,
  {
    label: string;
    variant: "default" | "success" | "warning" | "destructive" | "secondary";
  }
> = {
  pending: { label: "排队中", variant: "secondary" },
  running: { label: "运行中", variant: "default" },
  completed: { label: "已完成", variant: "success" },
  failed: { label: "失败", variant: "destructive" },
  dead: { label: "死信", variant: "destructive" },
  rolled_back: { label: "已回滚", variant: "warning" },
};

interface JobTableProps {
  jobs: JobRun[];
  busyId: string | null;
  trackedId: string | null;
  onSelect: (job: JobRun) => void;
  onTrack: (job: JobRun) => void;
  onRollback: (job: JobRun) => void;
}

export function JobTable({
  jobs,
  busyId,
  trackedId,
  onSelect,
  onTrack,
  onRollback,
}: JobTableProps) {
  if (jobs.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-300 bg-white py-14 text-sm text-slate-500"
        data-testid="jobs-empty"
      >
        <p>暂无任务记录</p>
        <p className="text-xs text-slate-400">
          触发巩固或导出后，任务会出现在这里。
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3">任务</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3">创建时间</th>
              <th className="px-4 py-3">结束时间</th>
              <th className="px-4 py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {jobs.map((job) => {
              const status = STATUS_META[job.status] ?? {
                label: job.status,
                variant: "secondary" as const,
              };
              const canRollback = job.status === "completed";
              const trackable = ["pending", "running"].includes(job.status);
              return (
                <tr
                  key={job.id}
                  data-testid="job-row"
                  data-job-id={job.id}
                  className="align-top hover:bg-slate-50"
                >
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      className="text-left font-medium text-slate-900 hover:text-indigo-600"
                      onClick={() => onSelect(job)}
                    >
                      {JOB_TYPE_LABELS[job.type] ?? job.type}
                    </button>
                    <p className="mt-0.5 max-w-[24rem] truncate text-xs text-slate-400">
                      {job.id}
                    </p>
                    {job.error ? (
                      <p className="mt-1 max-w-[28rem] text-xs text-red-600">
                        {job.error}
                      </p>
                    ) : null}
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant={status.variant}>{status.label}</Badge>
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-500">
                    {formatDateTime(job.created_at)}
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-500">
                    {formatDateTime(job.finished_at)}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      {trackable ? (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => onTrack(job)}
                          disabled={busyId === job.id}
                        >
                          {trackedId === job.id ? (
                            <Loader2
                              className="h-3.5 w-3.5 animate-spin"
                              aria-hidden="true"
                            />
                          ) : (
                            <Radio className="h-3.5 w-3.5" aria-hidden="true" />
                          )}
                          {trackedId === job.id ? "跟踪中" : "跟踪进度"}
                        </Button>
                      ) : null}
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => onRollback(job)}
                        disabled={!canRollback || busyId === job.id}
                      >
                        {busyId === job.id ? (
                          <RefreshCw
                            className="h-3.5 w-3.5 animate-spin"
                            aria-hidden="true"
                          />
                        ) : (
                          <RotateCcw
                            className="h-3.5 w-3.5"
                            aria-hidden="true"
                          />
                        )}
                        回滚
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
