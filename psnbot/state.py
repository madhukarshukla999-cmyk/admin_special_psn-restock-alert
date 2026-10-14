from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class State:
    """Remembers the last known stock status of every item, persisted as JSON."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
        # True once the first full check has recorded stock (older files: having items counts).
        self.seeded: bool = bool(data.get("seeded", bool(data.get("items"))))
        self.items: dict[str, dict] = data.get("items", {})
        self.store_errors: dict[str, dict] = data.get("store_errors", {})
        self.known: dict[str, dict[str, str]] = data.get("known", {})
        # User settings from the Telegram menu (muted stores) + polling offset
        self.settings: dict = data.get("settings", {})
        # Restock events (see history.py)
        self.history: list[dict] = data.get("history", [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"seeded": self.seeded, "items": self.items, "store_errors": self.store_errors,
             "known": self.known, "settings": self.settings, "history": self.history},
            indent=1, ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:   # utf-8: Windows default can't write ₹
            f.write(payload)
        os.replace(tmp, self.path)   # atomic write

    @property
    def is_new(self) -> bool:
        """True until the first full check has run (that check is silent + sends a summary)."""
        return not self.seeded
