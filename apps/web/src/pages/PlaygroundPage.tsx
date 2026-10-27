import { useMutation, useQuery } from "@tanstack/react-query";
import { FlaskConical, Loader2, Search } from "lucide-react";
import { useEffect, useState } from "react";

import { SearchInspector } from "../components/playground/SearchInspector";
import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { api, getErrorMessage } from "../lib/api";
import type { SearchResponse, WorkspaceListResponse } from "../lib/types";

type SearchMode = "balanced" | "recent" | "deep" | "exact";

export function PlaygroundPage() {
  const [workspaceId, setWorkspaceId] = useState("");
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("balanced");
  const [limit, setLimit] = useState("8");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const workspacesQuery = useQuery({
    queryKey: ["workspaces", "playground"],
    queryFn: () => api<WorkspaceListResponse>("/workspaces?limit=200"),
    retry: false,
  });

  useEffect(() => {
    if (!workspaceId && workspacesQuery.data?.items[0]) {
      setWorkspaceId(workspacesQuery.data.items[0].id);
    }
  }, [workspaceId, workspacesQuery.data]);

  const searchMutation = useMutation({
    mutationFn: () =>
      api<SearchResponse>("/search", {
        method: "POST",
        json: {
          workspace_id: workspaceId,
          query: query.trim(),
          mode,
          limit: Number(limit),
        },
      }),
    onSuccess: (data) => {
      setResult(data);
      setError(null);
    },
    onError: (caught) => setError(getErrorMessage(caught)),
  });

  const submit = () => {
    setError(null);
    if (!workspaceId) {
      setError("请先选择工作区");
      return;
    }
    if (!query.trim()) {
      setError("请输入检索语句");
      return;
    }
    searchMutation.mutate();
  };

  return (
    <div className="space-y-5">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-slate-900">
          <FlaskConical
            className="h-6 w-6 text-violet-500"
            aria-hidden="true"
          />
          检索调试台
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          并排查看四路召回排名、RRF 融合分数、最终上下文与降级状态。
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>检索参数</CardTitle>
          <CardDescription>
            调用真实 <code>/search</code> 接口；不展示原始内部元数据或密钥。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="space-y-1">
              <Label htmlFor="playground-workspace">工作区</Label>
              <select
                id="playground-workspace"
                data-testid="playground-workspace"
                className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-none"
                value={workspaceId}
                onChange={(event) => setWorkspaceId(event.target.value)}
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
              <Label htmlFor="playground-mode">模式</Label>
              <select
                id="playground-mode"
                className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-none"
                value={mode}
                onChange={(event) => setMode(event.target.value as SearchMode)}
              >
                <option value="balanced">balanced</option>
                <option value="recent">recent</option>
                <option value="deep">deep</option>
                <option value="exact">exact</option>
              </select>
            </label>

            <label className="space-y-1">
              <Label htmlFor="playground-limit">返回条数</Label>
              <Input
                id="playground-limit"
                type="number"
                min={1}
                max={50}
                value={limit}
                onChange={(event) => setLimit(event.target.value)}
              />
            </label>

            <div className="flex items-end">
              <Button
                className="w-full"
                onClick={submit}
                disabled={searchMutation.isPending}
              >
                {searchMutation.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                ) : (
                  <Search className="h-4 w-4" aria-hidden="true" />
                )}
                检索
              </Button>
            </div>
          </div>

          <label className="block space-y-1">
            <Label htmlFor="playground-query">检索语句</Label>
            <Input
              id="playground-query"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submit();
              }}
              placeholder="输入检索语句"
              maxLength={1000}
            />
          </label>

          {error ? <Alert>{error}</Alert> : null}
        </CardContent>
      </Card>

      {searchMutation.isPending ? (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white py-14 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          正在执行四路召回…
        </div>
      ) : null}

      {result ? <SearchInspector result={result} /> : null}
    </div>
  );
}
