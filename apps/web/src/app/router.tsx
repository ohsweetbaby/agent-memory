import { Route, Routes } from "react-router-dom";

import { AppShell } from "../components/layout/AppShell";
import { RequireAuth } from "../components/layout/RequireAuth";
import { DashboardPage } from "../pages/DashboardPage";
import { JobsPage } from "../pages/JobsPage";
import { LoginPage } from "../pages/LoginPage";
import { MemoriesPage } from "../pages/MemoriesPage";
import { MemoryDetailPage } from "../pages/MemoryDetailPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { PlaygroundPage } from "../pages/PlaygroundPage";
import { ReviewPage } from "../pages/ReviewPage";
import { SettingsPage } from "../pages/SettingsPage";
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
        <Route path="memories" element={<MemoriesPage />} />
        <Route path="memories/:memoryId" element={<MemoryDetailPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="jobs" element={<JobsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="playground" element={<PlaygroundPage />} />
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
