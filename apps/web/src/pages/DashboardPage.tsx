import { useQuery } from "@tanstack/react-query";
import {
  Brain,
  CalendarPlus,
  ClipboardCheck,
  Database,
  Loader2,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/ui/card";
import { Alert } from "../components/ui/alert";
import { api, getErrorMessage } from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { JobListResponse, JobRun, OverviewStats } from "../lib/types";

const MEMORY_TYPES = [
  { key: "working", label: "工作记忆", color: "bg-sky-500" },
  { key: "episodic", label: "情景记忆", color: "bg-violet-500" },
  { key: "semantic", label: "语义记忆", color: "bg-indigo-500" },
  { key: "procedural", label: "程序记忆", color: "bg-emerald-500" },
  { key: "perceptual", label: "感知记忆", color: "bg-amber-500" },
] as const;

const MEMORY_SCOPES = [
  { key: "global", label: "全局", color: "bg-indigo-500" },
  { key: "agent", label: "智能体", color: "bg-violet-500" },
  { key: "user", label: "用户", color: "bg-emerald-500" },
  { key: "session", label: "会话", color: "bg-amber-500" },
] as const;

const JOB_TYPE_LABELS: Record<string, string> = {
  consolidate_session: "会话巩固",
  consolidate_nightly: "夜间巩固",
  export: "导出",
};

const JOB_STATUS: Record<
  string,
  { label: string; variant: "default" | "success" | "warning" | "destructive" | "secondary" }
> = {
  pending: { label: "排队中", variant: "secondary" },
  running: { label: "运行中", variant: "default" },
  completed: { label: "已完成", variant: "success" },
  failed: { label: "失败", variant: "destructive" },
  dead: { label: "死信", variant: "destructive" },
  rolled_back: { label: "已回滚", variant: "warning" },
};

interface StatCardProps {
  label: string;
  value: ReactNode;
  testId: string;
  hint: string;
  icon: LucideIcon;
  tone: "indigo" | "emerald" | "amber" | "sky";
  accent?: boolean;
}

const TONE_CLASSES: Record<StatCardProps["tone"], string> = {
  indigo: "bg-indigo-50 text-indigo-600",
  emerald: "bg-emerald-50 text-emerald-600",
  amber: "bg-amber-50 text-amber-600",
  sky: "bg-sky-50 text-sky-600",
};

function StatCard({
  label,
  value,
  testId,
  hint,
  icon: Icon,
  tone,
  accent = false,
}: StatCardProps) {
  return (
    <Card className={accent ? "border-amber-300" : undefined}>
      <CardContent className="flex items-start justify-between gap-3 py-5">
        <div className="min-w-0">
          <p className="text-sm text-slate-500">{label}</p>
          <p
            className="mt-1 text-3xl font-semibold tracking-tight text-slate-900"
            data-testid={testId}
          >
            {value}
          </p>
          <p className="mt-1 text-xs text-slate-400">{hint}</p>
        </div>
        <span
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${TONE_CLASSES[tone]}`}
        >
          <Icon className="h-5 w-5" aria-hidden="true" />
        </span>
      </CardContent>
    </Card>
  );
}

function JobHealth({ jobs }: { jobs: JobRun[] }) {
  const failed = jobs.filter((job) =>
    ["failed", "dead"].includes(job.status),
  ).length;
  const running = jobs.filter((job) =>
    ["pending", "running"].includes(job.status),
  ).length;

  const summary =
    failed > 0
      ? { label: "有失败任务", variant: "destructive" as const }
      : running > 0
        ? { label: "任务运行中", variant: "default" as const }
        : { label: "队列正常", variant: "success" as const };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={summary.variant} data-testid="job-health">
          {summary.label}
        </Badge>
        <span className="text-sm text-slate-500">
          运行中{" "}
          <strong className="text-slate-800" data-testid="stat-running-jobs">
            {running}
          </strong>
          ，失败{" "}
          <strong className="text-slate-800" data-testid="stat-failed-jobs">
            {failed}
          </strong>
        </span>
      </div>

      {jobs.length === 0 ? (
        <p className="text-sm text-slate-500">
          暂无任务记录。巩固、衰减与导出任务会出现在这里。
        </p>
      ) : (
        <ul className="divide-y divide-slate-100" data-testid="recent-jobs">
          {jobs.slice(0, 3).map((job) => {
            const status = JOB_STATUS[job.status] ?? {
              label: job.status,
              variant: "secondary" as const,
            };
            return (
              <li
                key={job.id}
                className="flex items-center justify-between gap-3 py-2 text-sm"
                data-testid="job-row"
              >
                <div className="min-w-0">
                  <p className="truncate text-slate-800">
                    {JOB_TYPE_LABELS[job.type] ?? job.type}
                  </p>
                  <p className="text-xs text-slate-400">
                    {formatDateTime(job.created_at)}
                  </p>
                </div>
                <Badge variant={status.variant}>{status.label}</Badge>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function DistributionBars({
  counts,
  total,
  items,
}: {
  counts: Record<string, number>;
  total: number;
  items: ReadonlyArray<{ key: string; label: string; color?: string }>;
}) {
  return (
    <div className="space-y-3">
      {items.map((item) => {
        const count = counts[item.key] ?? 0;
        const percent = total > 0 ? Math.round((count / total) * 100) : 0;
        return (
          <div key={item.key} className="space-y-1">
            <div className="flex items-center justify-between text-sm">
              <span className="text-slate-600">{item.label}</span>
              <span className="font-medium text-slate-900">
                {count}
                <span className="ml-1 text-xs font-normal text-slate-400">
                  {percent}%
                </span>
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div
                className={`h-full rounded-full ${item.color ?? "bg-slate-400"}`}
                style={{ width: `${percent}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function DashboardPage() {
  const statsQuery = useQuery({
    queryKey: ["stats", "overview"],
    queryFn: () => api<OverviewStats>("/stats/overview"),
  });
  const jobsQuery = useQuery({
    queryKey: ["jobs", "recent"],
    queryFn: () => api<JobListResponse>("/jobs?limit=20"),
    retry: false,
  });

  const stats = statsQuery.data;
  const updatedAt =
    statsQuery.dataUpdatedAt > 0
      ? formatDateTime(new Date(statsQuery.dataUpdatedAt))
      : "—";

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
            概览
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            记忆库、待审队列与后台任务的实时状态。
          </p>
        </div>
        <p
          className="text-xs text-slate-400"
          data-testid="dashboard-updated-at"
        >
          数据更新于 {updatedAt}
        </p>
      </header>

      {statsQuery.isPending ? (
        <div
          className="flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white py-16 text-sm text-slate-500"
          data-testid="dashboard-loading"
        >
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          正在加载概览…
        </div>
      ) : null}

      {statsQuery.isError ? (
        <Card>
          <CardContent className="space-y-3 py-8">
            <Alert>
              概览加载失败：
              {getErrorMessage(statsQuery.error)}
            </Alert>
            <Button
              variant="outline"
              onClick={() => void statsQuery.refetch()}
              disabled={statsQuery.isFetching}
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              重新加载
            </Button>
          </CardContent>
        </Card>
      ) : null}

      {stats ? (
        <>
          <section
            className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4"
            aria-label="核心指标"
          >
            <StatCard
              label="记忆总量"
              value={stats.total_memories}
              testId="stat-total-memories"
              hint="含历史版本，不含软删除"
              icon={Brain}
              tone="indigo"
            />
            <StatCard
              label="活跃记忆"
              value={stats.active_memories}
              testId="stat-active-memories"
              hint="当前可被检索的记忆"
              icon={Database}
              tone="emerald"
            />
            <StatCard
              label="待审候选"
              value={stats.pending_review}
              testId="stat-pending-review"
              hint="需要人工确认后入库"
              icon={ClipboardCheck}
              tone="amber"
              accent={stats.pending_review > 0}
            />
            <StatCard
              label="近 30 天新增"
              value={stats.created_last_30_days}
              testId="stat-created-last-30-days"
              hint="按创建时间统计"
              icon={CalendarPlus}
              tone="sky"
            />
          </section>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Card data-testid="type-distribution">
              <CardHeader>
                <CardTitle>类型分布</CardTitle>
                <CardDescription>按记忆类型统计当前存量</CardDescription>
              </CardHeader>
              <CardContent>
                <DistributionBars
                  counts={stats.by_type}
                  total={stats.total_memories}
                  items={MEMORY_TYPES}
                />
              </CardContent>
            </Card>

            <Card data-testid="job-health-card">
              <CardHeader>
                <CardTitle>任务健康</CardTitle>
                <CardDescription>最近 20 条后台任务</CardDescription>
              </CardHeader>
              <CardContent>
                {jobsQuery.isPending ? (
                  <p className="flex items-center gap-2 text-sm text-slate-500">
                    <Loader2
                      className="h-4 w-4 animate-spin"
                      aria-hidden="true"
                    />
                    正在读取任务状态…
                  </p>
                ) : null}
                {jobsQuery.isError ? (
                  <Alert variant="info">
                    任务状态暂时不可用：{getErrorMessage(jobsQuery.error)}
                  </Alert>
                ) : null}
                {jobsQuery.data ? (
                  <JobHealth jobs={jobsQuery.data.items} />
                ) : null}
              </CardContent>
            </Card>
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>作用域分布</CardTitle>
                <CardDescription>记忆在全局/智能体/用户/会话间的分布</CardDescription>
              </CardHeader>
              <CardContent>
                <DistributionBars
                  counts={stats.by_scope}
                  total={stats.total_memories}
                  items={MEMORY_SCOPES}
                />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>存储统计</CardTitle>
                <CardDescription>会话、实体与状态计数</CardDescription>
              </CardHeader>
              <CardContent className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                <div className="rounded-lg bg-slate-50 p-3">
                  <p className="text-xs text-slate-500">会话</p>
                  <p
                    className="mt-1 text-xl font-semibold text-slate-900"
                    data-testid="stat-total-episodes"
                  >
                    {stats.total_episodes}
                  </p>
                </div>
                <div className="rounded-lg bg-slate-50 p-3">
                  <p className="text-xs text-slate-500">实体</p>
                  <p
                    className="mt-1 text-xl font-semibold text-slate-900"
                    data-testid="stat-total-entities"
                  >
                    {stats.total_entities}
                  </p>
                </div>
                <div className="rounded-lg bg-slate-50 p-3">
                  <p className="text-xs text-slate-500">归档</p>
                  <p className="mt-1 text-xl font-semibold text-slate-900">
                    {stats.by_status.archived ?? 0}
                  </p>
                </div>
                <div className="rounded-lg bg-slate-50 p-3">
                  <p className="text-xs text-slate-500">已取代</p>
                  <p className="mt-1 text-xl font-semibold text-slate-900">
                    {stats.by_status.superseded ?? 0}
                  </p>
                </div>
              </CardContent>
            </Card>
          </div>
        </>
      ) : null}
    </div>
  );
}
