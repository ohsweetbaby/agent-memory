import {
  Activity,
  Brain,
  BrainCircuit,
  ClipboardCheck,
  FlaskConical,
  LayoutDashboard,
  LogOut,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { useSession } from "../../lib/auth";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "概览", icon: LayoutDashboard, end: true },
  { to: "/memories", label: "记忆浏览器", icon: Brain },
  { to: "/review", label: "待审队列", icon: ClipboardCheck },
  { to: "/jobs", label: "任务中心", icon: Activity },
  { to: "/settings", label: "设置", icon: Settings },
  { to: "/playground", label: "检索调试台", icon: FlaskConical },
];

export function AppShell() {
  const { admin, logout } = useSession();
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await logout();
      navigate("/login", { replace: true });
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-50" data-testid="app-shell">
      <aside className="border-b border-slate-800 bg-slate-900 text-slate-200 md:fixed md:inset-y-0 md:left-0 md:flex md:w-60 md:flex-col md:border-b-0 md:border-r">
        <div className="flex items-center gap-2 px-5 py-4">
          <BrainCircuit className="h-6 w-6 text-indigo-400" aria-hidden="true" />
          <div>
            <p className="text-sm font-semibold text-white">Agent Memory</p>
            <p className="text-xs text-slate-400">本地记忆管理台</p>
          </div>
        </div>

        <nav
          className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col md:overflow-visible md:pb-0"
          aria-label="主导航"
        >
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    "flex shrink-0 items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                    isActive
                      ? "bg-indigo-600 text-white"
                      : "text-slate-300 hover:bg-slate-800 hover:text-white",
                  )
                }
              >
                <Icon className="h-4 w-4" aria-hidden="true" />
                {item.label}
              </NavLink>
            );
          })}
        </nav>

        <div className="mt-auto hidden border-t border-slate-800 px-3 py-3 md:block">
          <div className="mb-1 px-2 text-xs text-slate-400">
            当前管理员：
            <span className="ml-1 text-slate-200">
              {admin?.username ?? "unknown"}
            </span>
          </div>
          <Button
            variant="ghost"
            className="w-full justify-start text-slate-300 hover:bg-slate-800 hover:text-white"
            onClick={handleLogout}
            disabled={loggingOut}
          >
            <LogOut className="h-4 w-4" aria-hidden="true" />
            {loggingOut ? "正在退出…" : "退出登录"}
          </Button>
        </div>
      </aside>

      <div className="md:pl-60">
        <div className="flex items-center justify-between border-b border-slate-200 bg-white px-5 py-3 md:hidden">
          <span className="text-sm font-semibold text-slate-900">
            Agent Memory
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={handleLogout}
            disabled={loggingOut}
          >
            <LogOut className="h-4 w-4" aria-hidden="true" />
            退出登录
          </Button>
        </div>
        <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
