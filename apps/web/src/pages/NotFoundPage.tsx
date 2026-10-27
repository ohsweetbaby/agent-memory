import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-slate-50 px-4 text-center">
      <p className="text-6xl font-semibold tracking-tight text-slate-300">404</p>
      <h1 className="text-xl font-semibold text-slate-900">页面不存在</h1>
      <p className="text-sm text-slate-500">
        请检查地址，或返回概览继续操作。
      </p>
      <Link
        to="/"
        className="inline-flex h-10 items-center justify-center rounded-md bg-indigo-600 px-4 text-sm font-medium text-white transition-colors hover:bg-indigo-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2"
      >
        返回概览
      </Link>
    </div>
  );
}
