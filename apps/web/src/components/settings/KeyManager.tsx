import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Copy,
  KeyRound,
  Loader2,
  Plus,
  ShieldOff,
  Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";

import { api, getErrorMessage } from "../../lib/api";
import { formatDateTime } from "../../lib/format";
import type {
  ApiKeyCreateResponse,
  ApiKeyListResponse,
  WorkspaceRecord,
} from "../../lib/types";
import { Alert } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { ConfirmDialog } from "../ui/confirm-dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";

const SCOPE_LABELS: Record<string, string> = {
  read: "读",
  write: "写",
  admin: "管理",
};

interface KeyManagerProps {
  workspaces: WorkspaceRecord[];
}

export function KeyManager({ workspaces }: KeyManagerProps) {
  const queryClient = useQueryClient();
  const [workspaceId, setWorkspaceId] = useState("");
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  const [rateLimit, setRateLimit] = useState("");
  const [createdKey, setCreatedKey] = useState<ApiKeyCreateResponse | null>(
    null,
  );
  const [revokeTarget, setRevokeTarget] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!workspaceId && workspaces[0]) setWorkspaceId(workspaces[0].id);
  }, [workspaceId, workspaces]);

  const keysQuery = useQuery({
    queryKey: ["keys", "list"],
    queryFn: () => api<ApiKeyListResponse>("/keys?limit=200"),
    retry: false,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      api<ApiKeyCreateResponse>("/keys", {
        method: "POST",
        json: {
          workspace_id: workspaceId,
          name: name.trim(),
          scopes,
          rate_limit: rateLimit.trim() ? Number(rateLimit) : null,
        },
      }),
    onSuccess: (created) => {
      setCreatedKey(created);
      setCopied(false);
      setName("");
      setRateLimit("");
      void queryClient.invalidateQueries({ queryKey: ["keys"] });
    },
    onError: (caught) => setError(getErrorMessage(caught)),
  });

  const revokeMutation = useMutation({
    mutationFn: (id: string) =>
      api<void>(`/keys/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setRevokeTarget(null);
      void queryClient.invalidateQueries({ queryKey: ["keys"] });
    },
    onError: (caught) => setError(getErrorMessage(caught)),
  });

  const toggleScope = (scope: string) => {
    setScopes((current) =>
      current.includes(scope)
        ? current.filter((item) => item !== scope)
        : [...current, scope],
    );
  };

  const copyKey = async () => {
    if (!createdKey) return;
    try {
      await navigator.clipboard.writeText(createdKey.key);
      setCopied(true);
    } catch {
      setError("复制失败，请手动选择密钥文本");
    }
  };

  const keys = keysQuery.data?.items ?? [];

  return (
    <div data-testid="key-manager" className="space-y-4">
      {createdKey ? (
        <div
          className="space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-4"
          data-testid="api-key-once"
        >
          <div className="flex flex-wrap items-center gap-2">
            <KeyRound className="h-4 w-4 text-amber-700" aria-hidden="true" />
            <p className="text-sm font-semibold text-amber-900">
              请立即复制保存：完整密钥只显示一次
            </p>
            <Badge variant="warning">仅此一次</Badge>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              readOnly
              value={createdKey.key}
              aria-label="新 API Key 明文"
              className="font-mono text-xs"
              onFocus={(event) => event.currentTarget.select()}
            />
            <Button variant="outline" onClick={() => void copyKey()}>
              {copied ? (
                <Check className="h-4 w-4" aria-hidden="true" />
              ) : (
                <Copy className="h-4 w-4" aria-hidden="true" />
              )}
              {copied ? "已复制" : "复制"}
            </Button>
          </div>
          <p className="text-xs text-amber-800">
            关闭或刷新页面后无法再次查看；如遗失请吊销后重新创建。
          </p>
        </div>
      ) : null}

      {error ? <Alert>{error}</Alert> : null}

      <div className="grid grid-cols-1 gap-3 rounded-lg border border-slate-200 bg-slate-50/60 p-4 sm:grid-cols-2">
        <label className="space-y-1">
          <Label htmlFor="key-workspace">工作区</Label>
          <select
            id="key-workspace"
            className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-none"
            value={workspaceId}
            onChange={(event) => setWorkspaceId(event.target.value)}
          >
            <option value="">选择工作区</option>
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.name}
              </option>
            ))}
          </select>
        </label>

        <label className="space-y-1">
          <Label htmlFor="key-name">Key 名称</Label>
          <Input
            id="key-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="例如：Cursor 本地"
          />
        </label>

        <fieldset className="space-y-1 sm:col-span-2">
          <legend className="text-sm font-medium text-slate-700">权限范围</legend>
          <div className="flex flex-wrap gap-3 pt-1">
            {Object.entries(SCOPE_LABELS).map(([scope, label]) => (
              <label
                key={scope}
                className="flex items-center gap-2 text-sm text-slate-700"
              >
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                  checked={scopes.includes(scope)}
                  onChange={() => toggleScope(scope)}
                />
                {label}
                <span className="text-xs text-slate-400">{scope}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <label className="space-y-1">
          <Label htmlFor="key-rate-limit">每分钟限流（可选）</Label>
          <Input
            id="key-rate-limit"
            type="number"
            min={1}
            value={rateLimit}
            onChange={(event) => setRateLimit(event.target.value)}
            placeholder="120"
          />
        </label>

        <div className="flex items-end">
          <Button
            onClick={() => {
              setError(null);
              createMutation.mutate();
            }}
            disabled={
              createMutation.isPending ||
              !workspaceId ||
              !name.trim() ||
              scopes.length === 0
            }
          >
            {createMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Plus className="h-4 w-4" aria-hidden="true" />
            )}
            创建 API Key
          </Button>
        </div>
      </div>

      {keysQuery.isPending ? (
        <p className="flex items-center gap-2 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          正在加载 API Key…
        </p>
      ) : null}
      {keysQuery.isError ? (
        <Alert>API Key 加载失败：{getErrorMessage(keysQuery.error)}</Alert>
      ) : null}

      {keysQuery.data ? (
        keys.length === 0 ? (
          <p className="rounded-lg border border-dashed border-slate-300 px-3 py-6 text-center text-sm text-slate-500">
            还没有 API Key
          </p>
        ) : (
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
            {keys.map((key) => {
              const revoked = key.revoked_at !== null;
              return (
                <li
                  key={key.id}
                  data-testid="key-row"
                  className="flex flex-wrap items-center justify-between gap-2 px-4 py-3"
                >
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-900">
                      {key.name}
                      {revoked ? (
                        <Badge variant="destructive">已吊销</Badge>
                      ) : (
                        <Badge variant="success">有效</Badge>
                      )}
                    </p>
                    <p className="mt-0.5 font-mono text-xs text-slate-500">
                      mem_{key.prefix}_••••
                    </p>
                    <p className="mt-0.5 text-xs text-slate-400">
                      权限 {key.scopes.join(" / ")} · 创建于{" "}
                      {formatDateTime(key.created_at)}
                      {key.last_used_at
                        ? ` · 最近使用 ${formatDateTime(key.last_used_at)}`
                        : ""}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setRevokeTarget(key.id)}
                    disabled={revoked || revokeMutation.isPending}
                  >
                    {revoked ? (
                      <ShieldOff className="h-3.5 w-3.5" aria-hidden="true" />
                    ) : (
                      <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                    )}
                    {revoked ? "已吊销" : "吊销"}
                  </Button>
                </li>
              );
            })}
          </ul>
        )
      ) : null}

      <ConfirmDialog
        open={revokeTarget !== null}
        title="确认吊销 API Key"
        description="吊销后使用该 Key 的客户端会立即失去访问权限；此操作不可撤销。"
        confirmLabel="确认吊销"
        destructive
        busy={revokeMutation.isPending}
        onConfirm={() => {
          if (revokeTarget) revokeMutation.mutate(revokeTarget);
        }}
        onCancel={() => setRevokeTarget(null)}
      />
    </div>
  );
}
