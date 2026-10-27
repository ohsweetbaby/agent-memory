import type { ReactNode } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/ui/card";

interface PlaceholderPageProps {
  title: string;
  description: string;
  children?: ReactNode;
}

/**
 * T17 只交付骨架；具体功能由后续任务替换。
 * 这里保留真实标题，保证导航与路由可达。
 */
export function PlaceholderPage({
  title,
  description,
  children,
}: PlaceholderPageProps) {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
          {title}
        </h1>
        <p className="mt-1 text-sm text-slate-500">{description}</p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>功能建设中</CardTitle>
          <CardDescription>
            本页将在后续任务中接入真实数据与操作。
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-slate-500">
          {children ?? "当前版本已提供导航与登录态保护，功能实现将在下一阶段交付。"}
        </CardContent>
      </Card>
    </div>
  );
}
