"""资产存储：内容哈希去重、大小限制与路径校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.assets import AssetStore


def test_asset_store_deduplicates_content(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    digest = store.put(b"hello world", mime="text/plain")

    assert len(digest) == 64
    assert store.exists(digest)
    assert store.read(digest) == b"hello world"
    assert store.put(b"hello world", mime="text/plain") == digest
    assert store.path_for(digest).parent == tmp_path / digest[:2]


def test_asset_store_rejects_oversize_and_bad_digest(tmp_path: Path) -> None:
    store = AssetStore(tmp_path, max_bytes=4)
    with pytest.raises(ValueError, match="大小"):
        store.put(b"12345")
    with pytest.raises(ValueError, match="sha256"):
        store.path_for("../escape")
