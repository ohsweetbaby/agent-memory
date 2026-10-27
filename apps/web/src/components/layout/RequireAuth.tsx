import { Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useSession } from "../../lib/auth";
import { Alert } from "../ui/alert";
import { Button } from "../ui/button";

function FullPageLoading() {
  return (
    <div
      className="flex min-h-screen items-center justify-center gap-2 text-sm text-slate-500"
      data-testid="session-loading"
    >
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
      正在确认登录状态…
    </div>
  );
}

/** 未登录跳转登录页；会话服务不可达时给出重试入口。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { admin, loading, error, refresh } = useSession();
  const location = useLocation();

  if (loading) {
    return <FullPageLoading />;
  }

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <div className="w-full max-w-md space-y-3">
          <Alert>{error}</Alert>
          <Button onClick={() => void refresh()}>重试</Button>
        </div>
      </div>
    );
  }

  if (!admin) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }

  return <>{children}</>;
}
