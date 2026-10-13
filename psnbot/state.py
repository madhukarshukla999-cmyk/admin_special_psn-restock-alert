from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class State:
    """Remembers the last known stock status of every item, persisted as JSON."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.is_new = not self.path.exists()
        data = {}
        if not self.is_new:
            try:
                data = json.loads(self.path.read_text())
            except (ValueError, OSError):
                data = {}
                self.is_new = True
        self.items: dict[str, dict] = data.get("items", {})
        self.store_errors: dict[str, dict] = data.get("store_errors", {})
        self.known: dict[str, dict[str, str]] = data.get("known", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"items": self.items, "store_errors": self.store_errors, "known": self.known},
            indent=1, ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, self.path)   # atomic write
        self.is_new = False
