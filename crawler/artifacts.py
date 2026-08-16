"""Raw evidence storage for procedural source runs."""

from __future__ import annotations

import re
from pathlib import Path


class ArtifactRecorder:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.items = []

    def save_response(self, kind, name, response):
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-") or "page"
        path = self.root / safe_name
        path.write_bytes(response.body)
        item = {
            "kind": kind,
            "path": str(path),
            "url": response.url,
            "sha256": response.content_hash,
            "fetched_at": response.fetched_at,
        }
        self.items.append(item)
        return item
