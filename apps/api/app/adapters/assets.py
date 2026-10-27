"""本地资产存储（M1 只提供最小文件落盘能力）。

路径布局 ``{root}/{sha256[:2]}/{sha256}``；M3 的解析、ASR/VLM 与 S3 适配器
在此基础上扩展，接口保持不变。
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 50 * 1024 * 1024

#: M1 只接受常见图片/音频/文本，解析前仍需校验 magic number（M3）。
ALLOWED_MIME = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "audio/mpeg",
        "audio/wav",
        "audio/ogg",
        "text/plain",
        "application/pdf",
    }
)


class AssetStore:
    """按内容哈希去重的只追加存储。"""

    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allowed_mime: frozenset[str] = ALLOWED_MIME,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.allowed_mime = allowed_mime

    def validate(self, data: bytes, mime: str | None = None) -> None:
        if len(data) > self.max_bytes:
            raise ValueError(f"资产超过大小限制: {len(data)} > {self.max_bytes}")
        if mime is not None and mime not in self.allowed_mime:
            raise ValueError(f"不支持的资产类型: {mime}")

    def path_for(self, sha256: str) -> Path:
        digest = sha256.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("非法 sha256")
        return self.root / digest[:2] / digest

    def put(self, data: bytes, *, mime: str | None = None) -> str:
        """写入并返回 sha256；内容已存在时直接复用。"""
        self.validate(data, mime)
        digest = hashlib.sha256(data).hexdigest()
        destination = self.path_for(digest)
        if destination.exists():
            return digest

        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(temp_name, destination)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
        logger.info(
            "asset stored",
            extra={"sha256": digest, "bytes": len(data), "mime": mime},
        )
        return digest

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def read(self, sha256: str) -> bytes:
        return self.path_for(sha256).read_bytes()

    def delete(self, sha256: str) -> None:
        self.path_for(sha256).unlink(missing_ok=True)

    @staticmethod
    def guess_mime(filename: str) -> str | None:
        return mimetypes.guess_type(filename)[0]


__all__ = ["ALLOWED_MIME", "AssetStore", "DEFAULT_MAX_BYTES"]
