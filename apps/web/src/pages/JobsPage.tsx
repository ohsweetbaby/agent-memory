import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Loader2,
  Play,
  Radio,
  RefreshCw,
  Square,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { DryRunDiff } from "../components/jobs/DryRunDiff";
import { JobTable } from "../components/jobs/JobTable";
import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/ui/card";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { api, getErrorMessage } from "../lib/api";
import { formatDateTime } from "../lib/format";
import type {
  ConsolidationDiff,
  JobListResponse,
  JobRun,
  WorkspaceListResponse,
} from "../lib/types";

type ConsolidationType = "consolidate_nightly" | "consolidate_session";

interface LiveState {
  jobId: string;
  status: string;
  message: string;
  transport: "sse" | "polling";
}

function parseEvent<T>(event: MessageEvent<string>): T | null {
  try {
    return JSON.parse(event.data) as T;
  } catch {
    return null;
  }
}

export function JobsPage() {
  const queryClient = useQueryClient();
  const [workspaceId, setWorkspaceId] = useState("");
  const [taskType, setTaskType] = useState<ConsolidationType>(
    "consolidate_nightly",
  );
  const [sessionId, setSessionId] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [diff, setDiff] = useState<ConsolidationDiff | null>(null);
  const [selectedJob, setSelectedJob] = useState<JobRun | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<JobRun | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [banner, setBanner] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);
  const [trackedJobId, setTrackedJobId] = useState<string | null>(null);
  const [live, setLive] = useState<LiveState | null>(null);

  const workspacesQuery = useQuery({
    queryKey: ["workspaces", "jobs"],
    queryFn: () => api<WorkspaceListResponse>("/workspaces?limit=200"),
    retry: false,
  });

  useEffect(() => {
    if (workspaceId) return;
    const first = workspacesQuery.data?.items[0];
    if (first) setWorkspaceId(first.id);
  }, [workspaceId, workspacesQuery.data]);

  const jobsQueryString = useMemo(() => {
    const params = new URLSearchParams({ limit: "50" });
    if (workspaceId) params.set("workspace_id", workspaceId);
    if (typeFilter) params.set("type", typeFilter);
    return params.toString();
  }, [workspaceId, typeFilter]);

  const jobsQuery = useQuery({
    queryKey: ["jobs", "list", jobsQueryString],
    queryFn: () => api<JobListResponse>(`/jobs?${jobsQueryString}`),
    retry: false,
  });

  const refreshJobs = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    void queryClient.invalidateQueries({ queryKey: ["stats"] });
  }, [queryClient]);

  // SSE 订阅：失败/断开后自动退回 2 秒轮询，关闭页面时清理。
  useEffect(() => {
    if (!trackedJobId) {
      setLive(null);
      return;
    }
    let disposed = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const source = new EventSource(`/api/v1/jobs/${trackedJobId}/events`);

    const stopPolling = () => {
      if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const startPolling = () => {
      if (disposed || pollTimer !== null) return;
      setLive((current) => ({
        jobId: trackedJobId,
        status: current?.status ?? "unknown",
        message: current?.message ?? "SSE 已断开，正在轮询任务状态…",
        transport: "polling",
      }));
      const poll = async () => {
        try {
          const job = await api<JobRun>(`/jobs/${trackedJobId}`);
          if (disposed) return;
          setLive({
            jobId: trackedJobId,
            status: job.status,
            message: `轮询更新：${formatDateTime(job.finished_at ?? job.created_at)}`,
            transport: "polling",
          });
          if (["completed", "failed", "dead", "rolled_back"].includes(job.status)) {
            stopPolling();
            setTrackedJobId(null);
            refreshJobs();
          }
        } catch {
          if (!disposed) {
            setLive((current) =>
              current
                ? { ...current, message: "轮询失败，稍后自动重试…" }
                : current,
            );
          }
        }
      };
      void poll();
      pollTimer = setInterval(() => void poll(), 2000);
    };

    source.addEventListener("snapshot", (event) => {
      const snapshot = parseEvent<JobRun & { progress?: number }>(event);
      if (!snapshot || disposed) return;
      setLive({
        jobId: trackedJobId,
        status: snapshot.status,
        message: "已连接 SSE，等待进度更新…",
        transport: "sse",
      });
    });
    source.addEventListener("progress", (event) => {
      const payload = parseEvent<{
        status?: string;
        progress?: number;
        message?: string;
      }>(event);
      if (disposed) return;
      setLive({
        jobId: trackedJobId,
        status: payload?.status ?? "running",
        message:
          payload?.message ??
          (typeof payload?.progress === "number"
            ? `进度 ${payload.progress}%`
            : "任务执行中…"),
        transport: "sse",
      });
    });
    source.addEventListener("done", (event) => {
      if (disposed) return;
      const payload = parseEvent<{ status?: string }>(event);
      source.close();
      stopPolling();
      refreshJobs();
      setLive({
        jobId: trackedJobId,
        status: payload?.status ?? "completed",
        message: "任务已结束，列表已刷新",
        transport: "sse",
      });
      setTrackedJobId(null);
    });
    source.onerror = () => {
      source.close();
      startPolling();
    };

    return () => {
      disposed = true;
      source.close();
      stopPolling();
    };
  }, [trackedJobId, refreshJobs]);

  const triggerConsolidation = async (dryRun: boolean) => {
    if (!workspaceId) {
      setBanner({ kind: "error", text: "请先选择工作区" });
      return;
    }
    if (taskType === "consolidate_session" && !sessionId.trim()) {
      setBanner({ kind: "error", text: "会话巩固需要填写会话 ID" });
      return;
    }
    setRunning(true);
    setBanner(null);
    try {
      const params =
        taskType === "consolidate_session"
          ? { session_id: sessionId.trim() }
          : { workspace_id: workspaceId };
      const result = await api<ConsolidationDiff>("/jobs/run", {
        method: "POST",
        json: { type: taskType, params, dry_run: dryRun },
      });
      // 只有 dry-run 才展示“计划差异”；正式运行结果以任务列表与详情为准。
      setDiff(dryRun ? result : null);
      setBanner({
        kind: "success",
        text: dryRun
          ? "dry-run 完成：仅展示差异，没有写入任何数据"
          : result.job_run_id
            ? `任务已执行：${result.job_run_id}`
            : "任务完成：当前窗口没有可执行的变更",
      });
      refreshJobs();
    } catch (error) {
      setBanner({ kind: "error", text: getErrorMessage(error) });
    } finally {
      setRunning(false);
    }
  };

  const confirmRollback = async () => {
    const job = rollbackTarget;
    if (!job) return;
    setBusyId(job.id);
    try {
      const result = await api<{ restored: number }>(`/jobs/${job.id}/rollback`, {
        method: "POST",
      });
      setRollbackTarget(null);
      setBanner({
        kind: "success",
        text: `已回滚任务，恢复 ${result.restored} 条记忆`,
      });
      refreshJobs();
    } catch (error) {
      setBanner({ kind: "error", text: getErrorMessage(error) });
    } finally {
      setBusyId(null);
    }
  };

  const jobs = jobsQuery.data?.items ?? [];

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-slate-900">
            <Activity className="h-6 w-6 text-sky-500" aria-hidden="true" />
            任务中心
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            先 dry-run 查看差异，再执行巩固；回滚需要二次确认。
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void jobsQuery.refetch()}
          disabled={jobsQuery.isFetching}
        >
          <RefreshCw
            className={`h-4 w-4 ${jobsQuery.isFetching ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          刷新任务
        </Button>
      </header>

      {banner ? (
        banner.kind === "success" ? (
          <div
            className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700"
            role="status"
          >
            {banner.text}
          </div>
        ) : (
          <Alert>{banner.text}</Alert>
        )
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>触发巩固</CardTitle>
          <CardDescription>
            夜间巩固按工作区执行；会话巩固需要会话 ID。dry-run 只读取并调用模型，不写表。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="space-y-1">
              <Label htmlFor="jobs-workspace">工作区</Label>
              <select
                id="jobs-workspace"
                data-testid="jobs-workspace"
                className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-none"
                value={workspaceId}
                onChange={(event) => {
                  setWorkspaceId(event.target.value);
                  setDiff(null);
                  setSelectedJob(null);
                }}
              >
                <option value="">选择工作区</option>
                {(workspacesQuery.data?.items ?? []).map((workspace) => (
                  <option key={workspace.id} value={workspace.id}>
                    {workspace.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="space-y-1">
              <Label htmlFor="jobs-task-type">任务类型</Label>
              <select
                id="jobs-task-type"
                className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-none"
                value={taskType}
                onChange={(event) => {
                  setTaskType(event.target.value as ConsolidationType);
                  setDiff(null);
                }}
              >
                <option value="consolidate_nightly">夜间巩固</option>
                <option value="consolidate_session">会话巩固</option>
              </select>
            </label>

            {taskType === "consolidate_session" ? (
              <label className="space-y-1">
                <Label htmlFor="jobs-session-id">会话 ID</Label>
                <Input
                  id="jobs-session-id"
                  value={sessionId}
                  onChange={(event) => setSessionId(event.target.value)}
                  placeholder="session_id"
                />
              </label>
            ) : (
              <div className="hidden sm:block" />
            )}

            <div className="flex items-end gap-2">
              <Button
                variant="outline"
                onClick={() => void triggerConsolidation(true)}
                disabled={running}
              >
                {running ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                ) : (
                  <Play className="h-4 w-4" aria-hidden="true" />
                )}
                巩固 dry-run
              </Button>
              <Button
                onClick={() => void triggerConsolidation(false)}
                disabled={running}
              >
                <Play className="h-4 w-4" aria-hidden="true" />
                执行巩固
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {diff ? <DryRunDiff diff={diff} /> : null}

      {live ? (
        <div
          className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900"
          data-testid="job-live"
        >
          <div className="flex items-center gap-2">
            <Radio className="h-4 w-4" aria-hidden="true" />
            <span>
              {live.transport === "sse" ? "SSE" : "轮询"} · 状态{" "}
              {live.status} · {live.message}
            </span>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setTrackedJobId(null)}
          >
            <Square className="h-3.5 w-3.5" aria-hidden="true" />
            停止跟踪
          </Button>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">任务历史</h2>
        <label className="text-xs text-slate-500">
          <span className="mr-2">类型</span>
          <select
            className="h-8 rounded-md border border-slate-300 bg-white px-2 text-sm text-slate-800 focus-visible:border-indigo-500 focus-visible:outline-none"
            value={typeFilter}
            onChange={(event) => setTypeFilter(event.target.value)}
            aria-label="任务类型筛选"
          >
            <option value="">全部</option>
            <option value="consolidate_session">会话巩固</option>
            <option value="consolidate_nightly">夜间巩固</option>
            <option value="export">导出</option>
          </select>
        </label>
      </div>

      {jobsQuery.isPending ? (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          正在加载任务…
        </div>
      ) : null}

      {jobsQuery.isError ? (
        <Card>
          <CardContent className="space-y-3 py-6">
            <Alert>任务列表加载失败：{getErrorMessage(jobsQuery.error)}</Alert>
            <Button variant="outline" onClick={() => void jobsQuery.refetch()}>
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              重新加载
            </Button>
          </CardContent>
        </Card>
      ) : null}

      {jobsQuery.data ? (
        <JobTable
          jobs={jobs}
          busyId={busyId}
          trackedId={trackedJobId}
          onSelect={setSelectedJob}
          onTrack={(job) => {
            setSelectedJob(job);
            setTrackedJobId(job.id);
          }}
          onRollback={setRollbackTarget}
        />
      ) : null}

      {selectedJob ? (
        <Card data-testid="job-detail">
          <CardHeader className="flex-row items-start justify-between">
            <div>
              <CardTitle>任务详情</CardTitle>
              <CardDescription>{selectedJob.id}</CardDescription>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSelectedJob(null)}
            >
              关闭
            </Button>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-4 text-sm sm:grid-cols-2">
            <div>
              <p className="text-xs text-slate-500">参数</p>
              <pre className="mt-1 max-h-48 overflow-auto rounded-md bg-slate-50 p-3 text-xs text-slate-700">
                {JSON.stringify(selectedJob.params, null, 2)}
              </pre>
            </div>
            <div>
              <p className="text-xs text-slate-500">结果</p>
              <pre className="mt-1 max-h-48 overflow-auto rounded-md bg-slate-50 p-3 text-xs text-slate-700">
                {JSON.stringify(selectedJob.result, null, 2)}
              </pre>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <ConfirmDialog
        open={rollbackTarget !== null}
        title="确认回滚任务"
        description={
          <span>
            将撤销任务{" "}
            <code className="rounded bg-slate-100 px-1">
              {rollbackTarget?.id}
            </code>{" "}
            产生的记忆变更。该操作不可撤销，请确认任务确实选错后再继续。
          </span>
        }
        confirmLabel="确认回滚"
        destructive
        busy={busyId !== null}
        onConfirm={() => void confirmRollback()}
        onCancel={() => setRollbackTarget(null)}
      />
    </div>
  );
}
