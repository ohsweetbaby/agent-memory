import { ArrowLeft } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";

import { MemoryDetailDrawer } from "../components/memory/MemoryDetailDrawer";
import { Button } from "../components/ui/button";

export function MemoryDetailPage() {
  const { memoryId } = useParams<{ memoryId: string }>();
  const navigate = useNavigate();

  if (!memoryId) {
    return (
      <div className="space-y-4">
        <p className="text-sm text-slate-600">缺少记忆 ID。</p>
        <Button variant="outline" onClick={() => navigate("/memories")}>
          返回记忆浏览器
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Button variant="ghost" onClick={() => navigate("/memories")}>
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        返回记忆浏览器
      </Button>
      <MemoryDetailDrawer
        memoryId={memoryId}
        variant="page"
        onDeleted={() => navigate("/memories")}
        onSelectMemory={(id) => navigate(`/memories/${id}`)}
      />
    </div>
  );
}
