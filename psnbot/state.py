from __future__ import annotations

import asyncio
import json
import time
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class _FileStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8") if self.path.exists() else None
        except OSError:
            return None

    def save(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:   # utf-8: Windows default can't write ₹
            f.write(payload)
        os.replace(tmp, self.path)   # atomic write


class _PostgresStore:
    """One row in a Postgres table. Used on Heroku, whose disk is wiped on every restart."""

    def __init__(self, url: str):
        import psycopg   # only needed when DATABASE_URL is set
        self._psycopg = psycopg
        self.url = url
        self._conn = None
        with self._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS psnbot_state (id int PRIMARY KEY, data text NOT NULL)")

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg.connect(self.url, autocommit=True, connect_timeout=15)
        return self._conn.cursor()

    def _run(self, fn):
        try:
            with self._connect() as c:
                return fn(c)
        except self._psycopg.OperationalError:
            self._conn = None                      # connection dropped -> reconnect once
            with self._connect() as c:
                return fn(c)

    def load(self) -> str | None:
        def q(c):
            c.execute("SELECT data FROM psnbot_state WHERE id = 1")
            row = c.fetchone()
            return row[0] if row else None
        return self._run(q)

    def save(self, payload: str) -> None:
        self._run(lambda c: c.execute(
            "INSERT INTO psnbot_state (id, data) VALUES (1, %s) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data", (payload,)))


class State:
    """Remembers stock, history, users and settings. Saved to a JSON file, or to Postgres
    when DATABASE_URL is set (Heroku)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        url = os.environ.get("DATABASE_URL")
        self._store = _PostgresStore(url) if url else _FileStore(self.path)
        self.backend = "postgres" if url else "file"
        raw = self._store.load()
        data = {}
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
        elif url and self.path.exists():
            # First run on a database: bring over an existing state.json (e.g. uploaded from the PC)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                log.info("Imported %s into the database", self.path)
            except (ValueError, OSError):
                data = {}
        self._last = None
        self._saved_at = 0.0
        # True once the first full check has recorded stock (older files: having items counts).
        self.seeded: bool = bool(data.get("seeded", bool(data.get("items"))))
        self.items: dict[str, dict] = data.get("items", {})
        self.store_errors: dict[str, dict] = data.get("store_errors", {})
        self.known: dict[str, dict[str, str]] = data.get("known", {})
        # User settings from the Telegram menu (muted stores) + polling offset
        self.settings: dict = data.get("settings", {})
        # Restock events (see history.py)
        self.history: list[dict] = data.get("history", [])

    def dumps(self) -> str:
        return json.dumps(
            {"seeded": self.seeded, "items": self.items, "store_errors": self.store_errors,
             "known": self.known, "settings": self.settings, "history": self.history},
            indent=1, ensure_ascii=False,
        )

    def save(self) -> None:
        payload = self.dumps()
        if payload == self._last:
            return                       # nothing changed; skip the write
        try:
            self._store.save(payload)
            self._last = payload
            self._saved_at = time.time()
        except Exception:  # noqa: BLE001 - a failed save must not stop the checks
            log.exception("Saving state failed (%s); will retry on the next save", self.backend)

    async def save_soon(self, min_gap: float = 3.0) -> None:
        """Save without stalling the bot. Used on the checking path, which runs every couple of
        seconds: writing to Postgres blocks, and a stall there delays restock orders and alerts.
        At most one write per `min_gap` seconds; the heartbeat and shutdown always save in full."""
        if time.time() - self._saved_at < min_gap:
            return
        payload = self.dumps()
        if payload == self._last:
            self._saved_at = time.time()
            return
        self._saved_at = time.time()     # set first, so parallel checks don't all queue a write
        try:
            await asyncio.to_thread(self._store.save, payload)
            self._last = payload
        except Exception:  # noqa: BLE001
            log.exception("Saving state failed (%s); will retry on the next save", self.backend)

    @property
    def is_new(self) -> bool:
        """True until the first full check has run (that check is silent + sends a summary)."""
        return not self.seeded
