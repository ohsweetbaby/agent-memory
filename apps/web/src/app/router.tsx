import { Route, Routes } from "react-router-dom";

import { AppShell } from "../components/layout/AppShell";
import { RequireAuth } from "../components/layout/RequireAuth";
import { DashboardPage } from "../pages/DashboardPage";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { PlaceholderPage } from "../pages/PlaceholderPage";
import { SetupPage } from "../pages/SetupPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />

      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route
          path="memories"
          element={
            <PlaceholderPage
              title="记忆浏览器"
              description="浏览、筛选、检索和治理记忆。"
            />
          }
        />
        <Route
          path="review"
          element={
            <PlaceholderPage
              title="待审队列"
              description="审核抽取管线产生的低置信度候选。"
            />
          }
        />
        <Route
          path="jobs"
          element={
            <PlaceholderPage
              title="任务中心"
              description="查看巩固、遗忘、导出任务的进度与历史。"
            />
          }
        />
        <Route
          path="settings"
          element={
            <PlaceholderPage
              title="设置"
              description="配置模型 Provider、保留策略与 API Key。"
            />
          }
        />
        <Route
          path="playground"
          element={
            <PlaceholderPage
              title="检索调试台"
              description="对比不同检索模式的召回候选与最终上下文。"
            />
          }
        />
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
