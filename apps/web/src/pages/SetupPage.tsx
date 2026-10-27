import { BrainCircuit, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { ApiError, getErrorMessage } from "../lib/api";
import { useSession } from "../lib/auth";

export function SetupPage() {
  const { admin, loading, setupAdmin } = useSession();
  const navigate = useNavigate();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  if (!loading && admin) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setConflict(false);

    if (username.trim() === "") {
      setError("请填写管理员用户名");
      return;
    }
    if (password.length < 8) {
      setError("管理员密码至少需要 8 个字符");
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }

    setSubmitting(true);
    try {
      await setupAdmin(password, username.trim());
      navigate("/", { replace: true });
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "conflict") {
        setConflict(true);
        setError("管理员已存在，请直接登录");
      } else {
        setError(getErrorMessage(caught));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-600 text-white">
            <BrainCircuit className="h-6 w-6" aria-hidden="true" />
          </div>
          <div>
            <p className="text-lg font-semibold text-slate-900">Agent Memory</p>
            <p className="text-sm text-slate-500">首次启动 · 初始化管理员</p>
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h1 className="text-xl font-semibold text-slate-900">创建管理员</h1>
          <p className="mt-1 text-sm text-slate-500">
            管理台只支持一个管理员账号。密码使用 Argon2id 哈希保存，不会出现在浏览器存储中。
          </p>

          <form className="mt-5 space-y-4" onSubmit={handleSubmit} noValidate>
            {error ? (
              <Alert>
                <span>{error}</span>
                {conflict ? (
                  <Link
                    to="/login"
                    className="ml-2 font-medium underline underline-offset-2"
                  >
                    前往登录
                  </Link>
                ) : null}
              </Alert>
            ) : null}

            <div className="space-y-1.5">
              <Label htmlFor="setup-username">管理员用户名</Label>
              <Input
                id="setup-username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                spellCheck={false}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="setup-password">管理员密码</Label>
              <Input
                id="setup-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="new-password"
                placeholder="至少 8 个字符"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="setup-confirm">确认密码</Label>
              <Input
                id="setup-confirm"
                type="password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                autoComplete="new-password"
              />
            </div>

            <Button
              type="submit"
              className="w-full"
              disabled={submitting}
            >
              {submitting ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : null}
              {submitting ? "正在创建…" : "创建管理员"}
            </Button>
          </form>

          <p className="mt-4 text-center text-sm text-slate-500">
            已经有管理员？{" "}
            <Link
              to="/login"
              className="font-medium text-indigo-600 hover:text-indigo-700"
            >
              前往登录
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
