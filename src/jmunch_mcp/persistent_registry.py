"""Persistent, TTL-based handle registry.

Extends the in-memory `HandleRegistry` with an on-disk SQLite store so that
gateway handles survive process restarts — a requirement for a universal
proxy where handles might outlive individual conversations.

Design (jMRI-compliant):
  * The in-memory registry is the hot path; the DB is a fallback on miss.
  * We persist only the **source payload** (raw rows / text / JSON), not the
    live backend. Backends (TabularBackend's SQLite connection, TextBackend's
    index, JSONBackend's tree) rebuild themselves on first post-restart read.
  * TTL is per-handle. Default 3600s. A background sweeper removes expired
    rows every 60s.
  * Handle IDs preserve jMRI's opaque `h_<token>` shape.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .backends.jsontree import JSONBackend
from .backends.tabular import TabularBackend
from .backends.text import TextBackend
from .registry import DEFAULT_MAX_BYTES, DEFAULT_MAX_HANDLES, Handle, HandleRegistry

log = logging.getLogger("jmunch.persistent_registry")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS handles (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_blob BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handles_created ON handles(created_at);
"""


class PersistentHandleRegistry(HandleRegistry):
    def __init__(
        self,
        *,
        store_path: str | os.PathLike = "~/.jmunch/handles.db",
        ttl_seconds: int = 3600,
        max_handles: int = DEFAULT_MAX_HANDLES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        super().__init__(max_handles=max_handles, max_bytes=max_bytes)
        self._ttl = int(ttl_seconds)
        self._db_path = Path(os.path.expanduser(str(store_path)))
        self._db_lock = threading.Lock()
        self._con = self._open_db()
        self._sweep_task: asyncio.Task | None = None

    def _open_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self._db_path), timeout=2.0, isolation_level=None,
                              check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(_SCHEMA)
        return con

    def register(
        self,
        backend: Any,
        size_bytes: int,
        kind: str,
        *,
        source: Any = None,
        content_hash: str | None = None,
    ) -> Handle:
        h = super().register(backend, size_bytes, kind, content_hash=content_hash)
        if source is not None:
            self._persist(h.id, kind, size_bytes, source)
        return h

    def get(self, handle_id: str) -> Handle | None:
        h = super().get(handle_id)
        if h is not None:
            return h
        # Miss in memory: attempt DB rehydration.
        rehydrated = self._rehydrate(handle_id)
        return rehydrated

    def _persist(self, handle_id: str, kind: str, size_bytes: int, source: Any) -> None:
        try:
            blob = json.dumps(source, default=str).encode("utf-8")
        except (TypeError, ValueError) as e:
            log.warning("persist skipped for %s (not JSON-serializable): %s", handle_id, e)
            return
        with self._db_lock:
            try:
                self._con.execute(
                    "INSERT OR REPLACE INTO handles "
                    "(id, created_at, ttl_seconds, kind, size_bytes, source_blob) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (handle_id, time.time(), self._ttl, kind, int(size_bytes), blob),
                )
            except sqlite3.Error as e:
                log.debug("persist failed for %s: %s", handle_id, e)

    def _rehydrate(self, handle_id: str) -> Handle | None:
        with self._db_lock:
            try:
                row = self._con.execute(
                    "SELECT created_at, ttl_seconds, kind, size_bytes, source_blob "
                    "FROM handles WHERE id = ?",
                    (handle_id,),
                ).fetchone()
            except sqlite3.Error as e:
                log.debug("rehydrate query failed: %s", e)
                return None
        if row is None:
            return None
        created_at, ttl, kind, size_bytes, blob = row
        if ttl > 0 and (time.time() - created_at) > ttl:
            # Expired — drop and miss.
            self._delete(handle_id)
            return None
        try:
            source = json.loads(blob)
        except json.JSONDecodeError as e:
            log.warning("rehydrate skipped for %s (corrupt source): %s", handle_id, e)
            return None
        backend = _backend_from_source(kind, source)
        if backend is None:
            return None
        # Re-insert into the in-memory registry with the original ID.
        h = Handle(id=handle_id, backend=backend, size_bytes=int(size_bytes), kind=kind)
        with self._lock:
            self._store[handle_id] = h
            self._total_bytes += int(size_bytes)
            self._evict_if_needed()
        return h

    def _delete(self, handle_id: str) -> None:
        with self._db_lock:
            try:
                self._con.execute("DELETE FROM handles WHERE id = ?", (handle_id,))
            except sqlite3.Error:
                pass

    def sweep_expired(self, *, now: float | None = None) -> int:
        """Remove expired rows from the persistent store. Returns rows deleted."""
        t = now if now is not None else time.time()
        with self._db_lock:
            try:
                cur = self._con.execute(
                    "DELETE FROM handles WHERE ttl_seconds > 0 "
                    "AND (? - created_at) > ttl_seconds",
                    (t,),
                )
                return cur.rowcount or 0
            except sqlite3.Error as e:
                log.debug("sweep failed: %s", e)
                return 0

    async def start_sweeper(self, *, interval_s: int = 60) -> None:
        """Fire-and-forget sweeper task. Call once per process."""
        async def _loop():
            while True:
                try:
                    await asyncio.sleep(interval_s)
                    n = self.sweep_expired()
                    if n:
                        log.info("sweeper removed %d expired handles", n)
                except asyncio.CancelledError:
                    return
                except Exception as e:  # pragma: no cover
                    log.debug("sweeper iteration failed: %s", e)
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(_loop(), name="jmunch-sweeper")

    async def stop_sweeper(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sweep_task = None

    def close_db(self) -> None:
        with self._db_lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass


def _backend_from_source(kind: str, source: Any) -> Any | None:
    """Reconstruct the right backend from a persisted source blob."""
    try:
        if kind == "tabular":
            if not isinstance(source, list):
                return None
            return TabularBackend(source)
        if kind == "text":
            if not isinstance(source, str):
                return None
            return TextBackend(source)
        if kind == "json":
            return JSONBackend(source)
    except Exception as e:  # pragma: no cover
        log.warning("rehydrate failed to build %s backend: %s", kind, e)
    return None
