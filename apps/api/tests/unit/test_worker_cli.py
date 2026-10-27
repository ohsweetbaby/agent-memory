"""rebuild 参数校验与 Qdrant workspace 守卫（不依赖 Docker）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.qdrant import QdrantIndex
from app.worker_cli import main, run_rebuild


@pytest.mark.parametrize(
    "argv",
    [
        ["rebuild", "--target", "qdrant"],
        ["rebuild", "--target", "qdrant", "--workspace", "ws1", "--all"],
        ["rebuild", "--target", "qdrant", "--workspace", ""],
        ["rebuild", "--target", "qdrant", "--workspace", "   "],
    ],
)
def test_rebuild_cli_requires_explicit_non_empty_scope(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_run_rebuild_requires_explicit_scope() -> None:
    with pytest.raises(ValueError, match="必须指定"):
        run_rebuild("qdrant")
    with pytest.raises(ValueError, match="不能同时"):
        run_rebuild("qdrant", workspace="ws1", all_workspaces=True)
    with pytest.raises(ValueError, match="不能为空"):
        run_rebuild("qdrant", workspace="   ")


def test_qdrant_rejects_blank_workspace() -> None:
    index = QdrantIndex(
        "http://localhost:6333", provider="fake", model="fake-8", dim=8
    )
    try:
        with pytest.raises(ValueError, match="workspace_id"):
            index.list_ids("")
        with pytest.raises(ValueError, match="workspace_id"):
            index.list_ids("   ")
        with pytest.raises(ValueError, match="workspace_id"):
            index.delete_by_workspace("")
    finally:
        index.close()


def test_all_prints_cross_tenant_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, object] = {}

    def fake_run_rebuild(target: str, **kwargs: object) -> dict[str, int]:
        calls["target"] = target
        calls.update(kwargs)
        return {"qdrant": 0}

    monkeypatch.setattr(
        "app.worker_cli.build_runtime",
        lambda: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr("app.worker_cli.run_rebuild", fake_run_rebuild)

    assert main(["rebuild", "--target", "qdrant", "--all"]) == 0
    captured = capsys.readouterr()
    assert "跨租户" in captured.err
    assert calls["workspace"] is None
    assert calls["all_workspaces"] is True

    assert (
        main(["rebuild", "--target", "qdrant", "--workspace", " ws1 "]) == 0
    )
    assert calls["workspace"] == "ws1"
    assert calls["all_workspaces"] is False
