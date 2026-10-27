import { AlertTriangle, FileText, Gauge, Layers } from "lucide-react";

import type { SearchItem, SearchResponse } from "../../lib/types";
import { Badge } from "../ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";

const CHANNELS = [
  {
    key: "vector",
    label: "向量召回",
    testId: "channel-vector",
    whyKey: "vector_rank",
  },
  {
    key: "keyword",
    label: "BM25 关键词",
    testId: "channel-bm25",
    whyKey: "bm25_rank",
  },
  {
    key: "graph",
    label: "图扩展",
    testId: "channel-graph",
    whyKey: "graph_rank",
  },
  {
    key: "prior",
    label: "重要性 × 时近性",
    testId: "channel-prior",
    whyKey: "prior_rank",
  },
] as const;

function preview(content: string, length = 80): string {
  const text = content.replace(/\s+/g, " ").trim();
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function channelHits(items: SearchItem[], whyKey: string) {
  return items
    .map((item) => ({ item, rank: item.why[whyKey as keyof SearchItem["why"]] }))
    .filter(
      (entry): entry is { item: SearchItem; rank: number } =>
        typeof entry.rank === "number",
    )
    .sort((left, right) => left.rank - right.rank);
}

interface SearchInspectorProps {
  result: SearchResponse;
}

/** 检索调试台：四路排名、RRF/最终分、上下文与降级告警。 */
export function SearchInspector({ result }: SearchInspectorProps) {
  const degraded = new Set(result.degraded);

  return (
    <div className="space-y-4">
      {result.degraded.length > 0 ? (
        <div
          className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
          role="status"
          data-testid="degraded-warning"
        >
          <AlertTriangle
            className="mt-0.5 h-4 w-4 shrink-0"
            aria-hidden="true"
          />
          <div>
            <p className="font-medium">检索发生降级</p>
            <p className="text-xs">
              以下通道本次不可用：{result.degraded.join("、")}。其余通道结果仍会参与
              RRF 融合。
            </p>
          </div>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
          <p className="text-xs text-slate-500">模式</p>
          <p className="mt-0.5 font-medium text-slate-900">{result.stats.mode}</p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
          <p className="text-xs text-slate-500">耗时</p>
          <p className="mt-0.5 font-medium text-slate-900">
            {result.stats.latency_ms.toFixed(1)} ms
          </p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
          <p className="text-xs text-slate-500">候选 / 返回</p>
          <p className="mt-0.5 font-medium text-slate-900">
            {result.stats.candidates} / {result.stats.returned}
          </p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
          <p className="text-xs text-slate-500">上下文长度</p>
          <p className="mt-0.5 font-medium text-slate-900">
            {result.context_text.length} 字符
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-4">
        {CHANNELS.map((channel) => {
          const hits = channelHits(result.items, channel.whyKey);
          const isDegraded = degraded.has(channel.key);
          return (
            <div
              key={channel.key}
              data-testid={channel.testId}
              className="rounded-xl border border-slate-200 bg-white"
            >
              <div className="flex items-center justify-between gap-2 border-b border-slate-100 px-3 py-2">
                <p className="text-sm font-semibold text-slate-800">
                  {channel.label}
                </p>
                {isDegraded ? (
                  <Badge variant="warning">降级</Badge>
                ) : (
                  <Badge variant="secondary">
                    {result.stats.channels[channel.key] ?? hits.length} 条
                  </Badge>
                )}
              </div>
              <div className="max-h-72 space-y-2 overflow-auto px-3 py-2">
                {hits.length === 0 ? (
                  <p className="py-4 text-center text-xs text-slate-400">
                    {isDegraded ? "通道不可用，本次无结果" : "暂无命中"}
                  </p>
                ) : (
                  hits.map(({ item, rank }) => (
                    <div
                      key={`${channel.key}-${item.id}`}
                      className="rounded-md border border-slate-100 bg-slate-50 px-2 py-1.5"
                    >
                      <p className="flex items-center justify-between gap-2 text-xs text-slate-500">
                        <span className="font-mono">#{rank}</span>
                        <span>final {item.score.toFixed(4)}</span>
                      </p>
                      <p className="mt-1 break-words text-xs text-slate-700">
                        {preview(item.content)}
                      </p>
                    </div>
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>

      <Card data-testid="rrf-table">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Layers className="h-4 w-4 text-indigo-500" aria-hidden="true" />
            RRF 融合与最终排序
          </CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {result.items.length === 0 ? (
            <p className="py-4 text-center text-sm text-slate-500">
              没有检索结果
            </p>
          ) : (
            <table className="min-w-full divide-y divide-slate-200 text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="px-2 py-2">内容</th>
                  <th className="px-2 py-2">向量</th>
                  <th className="px-2 py-2">BM25</th>
                  <th className="px-2 py-2">图</th>
                  <th className="px-2 py-2">先验</th>
                  <th className="px-2 py-2">RRF</th>
                  <th className="px-2 py-2">final</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {result.items.map((item) => (
                  <tr key={item.id}>
                    <td className="max-w-md px-2 py-2">
                      <p className="break-words text-slate-800">
                        {preview(item.content, 100)}
                      </p>
                      <p className="text-xs text-slate-400">
                        {item.type} · {item.scope}
                        {item.historical ? " · 历史" : ""}
                      </p>
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">
                      {item.why.vector_rank ?? "—"}
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">
                      {item.why.bm25_rank ?? "—"}
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">
                      {item.why.graph_rank ?? "—"}
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">
                      {item.why.prior_rank ?? "—"}
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">
                      {item.why.rrf.toFixed(4)}
                    </td>
                    <td className="px-2 py-2 font-mono text-xs font-semibold text-indigo-700">
                      {item.score.toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card data-testid="final-context">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <FileText className="h-4 w-4 text-emerald-500" aria-hidden="true" />
            最终上下文
          </CardTitle>
        </CardHeader>
        <CardContent>
          {result.context_text ? (
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-50 p-3 text-xs text-slate-700">
              {result.context_text}
            </pre>
          ) : (
            <p className="text-sm text-slate-500">上下文为空</p>
          )}
          <p className="mt-2 flex items-center gap-1 text-xs text-slate-400">
            <Gauge className="h-3.5 w-3.5" aria-hidden="true" />
            仅展示召回内容与分值，不暴露内部元数据或密钥。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
