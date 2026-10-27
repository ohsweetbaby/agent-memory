import { BrainCircuit, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";

import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { getErrorMessage } from "../lib/api";
import { useSession } from "../lib/auth";

interface LocationState {
  from?: string;
}

export function LoginPage() {
  const { admin, loading, login } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!loading && admin) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    if (username.trim() === "" || password === "") {
      setError("请填写用户名和密码");
      return;
    }

    setSubmitting(true);
    try {
      await login(username.trim(), password);
      const state = location.state as LocationState | null;
      navigate(state?.from ?? "/", { replace: true });
    } catch (caught) {
      setError(getErrorMessage(caught));
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
            <p className="text-sm text-slate-500">登录本地记忆管理台</p>
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h1 className="text-xl font-semibold text-slate-900">登录</h1>
          <p className="mt-1 text-sm text-slate-500">
            使用首次启动时创建的管理员账号登录。
          </p>

          <form className="mt-5 space-y-4" onSubmit={handleSubmit} noValidate>
            {error ? <Alert>{error}</Alert> : null}

            <div className="space-y-1.5">
              <Label htmlFor="login-username">用户名</Label>
              <Input
                id="login-username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                spellCheck={false}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="login-password">密码</Label>
              <Input
                id="login-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
              />
            </div>

            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : null}
              {submitting ? "正在登录…" : "登录"}
            </Button>
          </form>

          <p className="mt-4 text-center text-sm text-slate-500">
            还没有管理员？{" "}
            <Link
              to="/setup"
              className="font-medium text-indigo-600 hover:text-indigo-700"
            >
              初始化
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
