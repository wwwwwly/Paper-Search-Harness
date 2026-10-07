from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4


class LocalArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def local_path(self, file_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", file_id):
            raise ValueError("invalid file identifier")
        return str(self.root / file_id[:2] / file_id)

    async def put(self, data: bytes) -> str:
        file_id = hashlib.sha256(data).hexdigest()
        path = Path(self.local_path(file_id))

        def write():
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                temporary = path.with_name(f".{uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(data)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)

        await asyncio.to_thread(write)
        return file_id

    async def read(self, file_id: str) -> bytes:
        return await asyncio.to_thread(Path(self.local_path(file_id)).read_bytes)
