import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database,
  Loader2,
  Pencil,
  RefreshCw,
  Server,
  Settings,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useState } from "react";

import { KeyManager } from "../components/settings/KeyManager";
import { ProviderForm } from "../components/settings/ProviderForm";
import { Alert } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/ui/card";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { api, getErrorMessage } from "../lib/api";
import type {
  ProviderConfig,
  ProviderListResponse,
  ProviderTestResult,
  WorkspaceListResponse,
} from "../lib/types";

const KIND_LABELS: Record<string, string> = {
  embedding: "嵌入模型",
  llm: "对话模型",
  reranker: "重排模型",
};

export function SettingsPage() {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<ProviderConfig | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProviderConfig | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<
    Record<string, ProviderTestResult>
  >({});
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);

  const providersQuery = useQuery({
    queryKey: ["providers"],
    queryFn: () => api<ProviderListResponse>("/providers"),
    retry: false,
  });
  const workspacesQuery = useQuery({
    queryKey: ["workspaces", "settings"],
    queryFn: () => api<WorkspaceListResponse>("/workspaces?limit=200"),
    retry: false,
  });

  const providers = providersQuery.data?.items ?? [];
  const workspaces = workspacesQuery.data?.items ?? [];
  const policy = providersQuery.data?.policy;

  const refreshProviders = () => {
    void queryClient.invalidateQueries({ queryKey: ["providers"] });
  };

  const testSavedProvider = async (provider: ProviderConfig) => {
    setTestingId(provider.id);
    setBanner(null);
    try {
      const result = await api<ProviderTestResult>(
        `/providers/${provider.id}/test`,
        { method: "POST" },
      );
      setTestResults((current) => ({ ...current, [provider.id]: result }));
    } catch (caught) {
      setTestResults((current) => ({
        ...current,
        [provider.id]: {
          ok: false,
          message: `连接失败：${getErrorMessage(caught)}`,
          latency_ms: 0,
          status_code: null,
        },
      }));
    } finally {
      setTestingId(null);
    }
  };

  const deleteProvider = async () => {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await api<void>(`/providers/${deleteTarget.id}`, { method: "DELETE" });
      if (editing?.id === deleteTarget.id) setEditing(null);
      setDeleteTarget(null);
      setBanner({ kind: "success", text: "已删除 Provider" });
      refreshProviders();
    } catch (caught) {
      setBanner({ kind: "error", text: getErrorMessage(caught) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-slate-900">
            <Settings className="h-6 w-6 text-slate-500" aria-hidden="true" />
            设置
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            管理模型 Provider、API Key，并查看遗忘/保留策略。
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            refreshProviders();
            void workspacesQuery.refetch();
          }}
          disabled={providersQuery.isFetching}
        >
          <RefreshCw
            className={`h-4 w-4 ${providersQuery.isFetching ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          刷新
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
          <CardTitle className="flex items-center gap-2">
            <Server className="h-4 w-4 text-indigo-500" aria-hidden="true" />
            模型 Provider
          </CardTitle>
          <CardDescription>
            密钥加密保存，列表不回显；连通性测试最多等待 5 秒。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <ProviderForm
            provider={editing}
            onSaved={() => {
              setEditing(null);
              setBanner({ kind: "success", text: "Provider 已保存" });
              refreshProviders();
            }}
            onCancelEdit={() => setEditing(null)}
          />

          {providersQuery.isPending ? (
            <p className="flex items-center gap-2 text-sm text-slate-500">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              正在加载 Provider…
            </p>
          ) : null}
          {providersQuery.isError ? (
            <Alert>Provider 加载失败：{getErrorMessage(providersQuery.error)}</Alert>
          ) : null}

          {providersQuery.data ? (
            providers.length === 0 ? (
              <p className="rounded-lg border border-dashed border-slate-300 px-3 py-6 text-center text-sm text-slate-500">
                还没有 Provider；填写上方表单即可创建。
              </p>
            ) : (
              <ul className="space-y-2">
                {providers.map((provider) => {
                  const result = testResults[provider.id];
                  return (
                    <li
                      key={provider.id}
                      data-testid="provider-row"
                      className="rounded-lg border border-slate-200 bg-white px-4 py-3"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-900">
                            {provider.name}
                            <Badge variant="secondary">
                              {KIND_LABELS[provider.kind] ?? provider.kind}
                            </Badge>
                            <Badge
                              variant={provider.enabled ? "success" : "outline"}
                            >
                              {provider.enabled ? "启用" : "停用"}
                            </Badge>
                            {/* 只展示掩码状态，绝不回显密钥。 */}
                            <Badge
                              variant={provider.has_secret ? "success" : "warning"}
                            >
                              {provider.has_secret ? "已配置密钥" : "未配置密钥"}
                            </Badge>
                          </p>
                          <p className="mt-1 break-all text-xs text-slate-500">
                            {provider.base_url ?? "未配置 Base URL"}
                            {provider.model ? ` · ${provider.model}` : ""}
                            {provider.dim ? ` · ${provider.dim} 维` : ""}
                          </p>
                        </div>
                        <div className="flex flex-wrap gap-1">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => void testSavedProvider(provider)}
                            disabled={testingId === provider.id}
                          >
                            {testingId === provider.id ? (
                              <Loader2
                                className="h-3.5 w-3.5 animate-spin"
                                aria-hidden="true"
                              />
                            ) : (
                              <RefreshCw
                                className="h-3.5 w-3.5"
                                aria-hidden="true"
                              />
                            )}
                            测试连接
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setEditing(provider)}
                          >
                            <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                            编辑
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setDeleteTarget(provider)}
                          >
                            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                            删除
                          </Button>
                        </div>
                      </div>
                      {result ? (
                        <p
                          className={`mt-2 rounded-md px-2 py-1 text-xs ${
                            result.ok
                              ? "bg-emerald-50 text-emerald-700"
                              : "bg-red-50 text-red-700"
                          }`}
                          data-testid="provider-row-test-result"
                        >
                          {result.message} · {result.latency_ms.toFixed(0)} ms
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-emerald-500" aria-hidden="true" />
            API Key 管理
          </CardTitle>
          <CardDescription>
            完整明文只在创建时显示一次；服务端仅保存哈希。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {workspacesQuery.isPending ? (
            <p className="flex items-center gap-2 text-sm text-slate-500">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              正在加载工作区…
            </p>
          ) : workspacesQuery.isError ? (
            <Alert>
              工作区加载失败：{getErrorMessage(workspacesQuery.error)}
            </Alert>
          ) : (
            <KeyManager workspaces={workspaces} />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="h-4 w-4 text-amber-500" aria-hidden="true" />
            遗忘与保留策略
          </CardTitle>
          <CardDescription>
            来自当前 API 运行配置；修改 .env 后重启服务生效。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {policy ? (
            <dl
              className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4"
              data-testid="retention-policy"
            >
              {[
                {
                  label: "归档阈值",
                  value: `保留率 < ${policy.decay_archive_threshold}`,
                },
                {
                  label: "软删除保留",
                  value: `${policy.purge_retention_days} 天`,
                },
                {
                  label: "待审保留",
                  value: `${policy.review_retention_days} 天`,
                },
                {
                  label: "审计保留",
                  value: `${policy.audit_retention_days} 天`,
                },
                {
                  label: "会话空闲巩固",
                  value: `${policy.consolidation_idle_minutes} 分钟`,
                },
                {
                  label: "夜间巩固时刻",
                  value: policy.consolidation_nightly,
                },
                {
                  label: "上下文预算",
                  value: `${policy.search_context_budget_tokens} tokens`,
                },
                {
                  label: "查询改写",
                  value: policy.search_query_rewrite ? "已开启" : "已关闭",
                },
              ].map((item) => (
                <div
                  key={item.label}
                  className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2"
                >
                  <dt className="text-xs text-slate-500">{item.label}</dt>
                  <dd className="mt-0.5 font-medium text-slate-900">
                    {item.value}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="text-sm text-slate-500">策略参数暂不可用。</p>
          )}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="确认删除 Provider"
        description={`删除「${deleteTarget?.name ?? ""}」后，使用该配置的任务将无法继续。`}
        confirmLabel="确认删除"
        destructive
        busy={busy}
        onConfirm={() => void deleteProvider()}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
