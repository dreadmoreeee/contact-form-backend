"""Optional logs: JSON lines (metadata only) and SQLite (with retention)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional


class JsonlLog:
    """Appends one JSON object per submission. Never contains field values."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=True, sort_keys=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class SQLiteLog:
    """Stores submissions (fields included) and deletes rows older than ``retention_days``.

    Old rows are purged when the log opens and then at most once an hour,
    on the next write.
    """

    def __init__(
        self,
        path: str,
        retention_days: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.retention = retention_days * 86400.0
        self._clock = clock
        self._lock = threading.Lock()
        self._next_purge = 0.0
        self._db = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS submissions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, "
                "form TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, "
                "ip TEXT, data TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS submissions_created ON submissions (created_at)"
            )
        self.purge()

    def add(
        self,
        form: str,
        outcome: str,
        reason: str,
        data: Mapping[str, str],
        ip: Optional[str] = None,
    ) -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO submissions (created_at, form, outcome, reason, ip, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, form, outcome, reason, ip, json.dumps(dict(data), ensure_ascii=False)),
            )
        if now >= self._next_purge:
            self.purge()

    def purge(self) -> int:
        """Delete rows older than the retention period. Returns how many."""
        now = self._clock()
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM submissions WHERE created_at < ?", (now - self.retention,)
            )
            self._next_purge = now + 3600.0
            return cur.rowcount

    def rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT id, created_at, form, outcome, reason, ip, data "
                "FROM submissions ORDER BY id"
            )
            names = [c[0] for c in cur.description]
            out = [dict(zip(names, row)) for row in cur.fetchall()]
        for row in out:
            row["data"] = json.loads(row["data"])
        return out

    def close(self) -> None:
        with self._lock:
            self._db.close()
