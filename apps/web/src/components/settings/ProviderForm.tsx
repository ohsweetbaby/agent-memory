import { Check, Loader2, RefreshCw, Save, Server, X } from "lucide-react";
import { useEffect, useState } from "react";

import { api, getErrorMessage } from "../../lib/api";
import type { ProviderConfig, ProviderTestResult } from "../../lib/types";
import { Alert } from "../ui/alert";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";

interface ProviderFormProps {
  provider: ProviderConfig | null;
  onSaved: () => void;
  onCancelEdit: () => void;
}

interface FormState {
  kind: string;
  name: string;
  baseUrl: string;
  model: string;
  dim: string;
  apiKey: string;
  enabled: boolean;
}

function initialForm(provider: ProviderConfig | null): FormState {
  return {
    kind: provider?.kind ?? "embedding",
    name: provider?.name ?? "",
    baseUrl: provider?.base_url ?? "",
    model: provider?.model ?? "",
    dim: provider?.dim ? String(provider.dim) : "",
    apiKey: "",
    enabled: provider?.enabled ?? true,
  };
}

/**
 * Provider 表单：API Key 只写不读；编辑时留空表示保留原密钥。
 * 测试连接对未保存表单使用 /providers/test，编辑已有项时后端复用已加密密钥。
 */
export function ProviderForm({
  provider,
  onSaved,
  onCancelEdit,
}: ProviderFormProps) {
  const [form, setForm] = useState<FormState>(() => initialForm(provider));
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<ProviderTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setForm(initialForm(provider));
    setTestResult(null);
    setError(null);
  }, [provider]);

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setTestResult(null);
  };

  const buildPayload = () => {
    const dim = form.dim.trim();
    return {
      kind: form.kind,
      name: form.name.trim(),
      base_url: form.baseUrl.trim() || null,
      model: form.model.trim() || null,
      dim: dim ? Number(dim) : null,
      extra: provider?.extra ?? {},
      enabled: form.enabled,
    };
  };

  const testConnection = async () => {
    setTesting(true);
    setError(null);
    try {
      const result = await api<ProviderTestResult>("/providers/test", {
        method: "POST",
        json: {
          provider_id: provider?.id ?? null,
          base_url: form.baseUrl.trim() || null,
          api_key: form.apiKey.trim() || null,
        },
      });
      setTestResult(result);
    } catch (caught) {
      setTestResult({
        ok: false,
        message: `连接失败：${getErrorMessage(caught)}`,
        latency_ms: 0,
        status_code: null,
      });
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const payload = buildPayload();
      if (form.apiKey.trim()) {
        Object.assign(payload, { api_key: form.apiKey.trim() });
      }
      if (provider) {
        await api<ProviderConfig>(`/providers/${provider.id}`, {
          method: "PATCH",
          json: payload,
        });
      } else {
        await api<ProviderConfig>("/providers", {
          method: "POST",
          json: payload,
        });
        setForm(initialForm(null));
      }
      setTestResult(null);
      onSaved();
    } catch (caught) {
      setError(getErrorMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="rounded-lg border border-slate-200 bg-slate-50/60 p-4"
      data-testid="provider-form"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Server className="h-4 w-4 text-slate-500" aria-hidden="true" />
          <h3 className="text-sm font-semibold text-slate-800">
            {provider ? `编辑 Provider：${provider.name}` : "新增 Provider"}
          </h3>
        </div>
        {provider ? (
          <Button variant="ghost" size="sm" onClick={onCancelEdit}>
            <X className="h-3.5 w-3.5" aria-hidden="true" />
            取消编辑
          </Button>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <Label htmlFor="provider-kind">类型</Label>
          <select
            id="provider-kind"
            className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-none"
            value={form.kind}
            onChange={(event) => update("kind", event.target.value)}
          >
            <option value="embedding">嵌入模型</option>
            <option value="llm">对话模型</option>
            <option value="reranker">重排模型</option>
          </select>
        </label>

        <label className="space-y-1">
          <Label htmlFor="provider-name">名称</Label>
          <Input
            id="provider-name"
            value={form.name}
            onChange={(event) => update("name", event.target.value)}
            placeholder="例如：OpenAI 主账号"
          />
        </label>

        <label className="space-y-1 sm:col-span-2">
          <Label htmlFor="provider-base-url">Base URL</Label>
          <Input
            id="provider-base-url"
            value={form.baseUrl}
            onChange={(event) => update("baseUrl", event.target.value)}
            placeholder="https://api.openai.com/v1"
            inputMode="url"
          />
        </label>

        <label className="space-y-1">
          <Label htmlFor="provider-model">模型</Label>
          <Input
            id="provider-model"
            value={form.model}
            onChange={(event) => update("model", event.target.value)}
            placeholder="text-embedding-3-small"
          />
        </label>

        <label className="space-y-1">
          <Label htmlFor="provider-dim">维度</Label>
          <Input
            id="provider-dim"
            type="number"
            min={1}
            value={form.dim}
            onChange={(event) => update("dim", event.target.value)}
            placeholder="1536"
          />
        </label>

        <label className="space-y-1 sm:col-span-2">
          <Label htmlFor="provider-api-key">API Key</Label>
          <Input
            id="provider-api-key"
            type="password"
            autoComplete="new-password"
            value={form.apiKey}
            onChange={(event) => update("apiKey", event.target.value)}
            placeholder={
              provider?.has_secret
                ? "已保存密钥；留空表示不修改"
                : "sk-...（保存后不再显示）"
            }
          />
          <p className="text-xs text-slate-500">
            密钥加密保存，列表与编辑框都不会回显；更新时留空表示不修改。
          </p>
        </label>

        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
            checked={form.enabled}
            onChange={(event) => update("enabled", event.target.checked)}
          />
          启用该 Provider
        </label>
      </div>

      {testResult ? (
        <div
          className={`mt-3 flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm ${
            testResult.ok
              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
              : "border-red-200 bg-red-50 text-red-700"
          }`}
          role="status"
          data-testid="provider-test-result"
        >
          {testResult.ok ? (
            <Check className="h-4 w-4" aria-hidden="true" />
          ) : (
            <X className="h-4 w-4" aria-hidden="true" />
          )}
          <span>{testResult.message}</span>
          <span className="text-xs opacity-70">
            耗时 {testResult.latency_ms.toFixed(0)} ms
            {testResult.status_code ? ` · HTTP ${testResult.status_code}` : ""}
          </span>
        </div>
      ) : null}

      {error ? <Alert className="mt-3">{error}</Alert> : null}

      <div className="mt-4 flex flex-wrap gap-2">
        <Button
          variant="outline"
          onClick={() => void testConnection()}
          disabled={testing || saving}
        >
          {testing ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
          )}
          {testing ? "正在测试…" : "测试连接"}
        </Button>
        <Button onClick={() => void save()} disabled={saving || testing}>
          {saving ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <Save className="h-4 w-4" aria-hidden="true" />
          )}
          {provider ? "保存修改" : "创建 Provider"}
        </Button>
      </div>
    </div>
  );
}
