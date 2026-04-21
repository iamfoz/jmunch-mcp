"""Shared SQLite metrics log for all running jmunch-mcp proxies.

Every proxy appends one row per forwarded tool call. The dashboard reads
from this same DB to build cumulative stats, per-upstream breakdowns, and
a recent-calls tail.

Concurrency model: SQLite in WAL mode tolerates many concurrent writers
fine for our volume (a few calls/sec/proxy at peak). Each proxy opens its
own connection. Writes are `INSERT` only; no schema mutations after init.

Failure model: metrics must **never** break the proxy. All public
functions swallow OSError / sqlite3.Error and log at debug level; callers
don't need to guard.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger("jmunch.metrics")


def default_db_path() -> Path:
    """`~/.jmunch/metrics.db` (override via `JMUNCH_METRICS_DB` env)."""
    override = os.environ.get("JMUNCH_METRICS_DB")
    if override:
        return Path(override)
    return Path.home() / ".jmunch" / "metrics.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    upstream TEXT NOT NULL,
    tool TEXT NOT NULL,
    request_bytes INTEGER NOT NULL DEFAULT 0,
    raw_bytes INTEGER NOT NULL DEFAULT 0,
    response_bytes INTEGER NOT NULL DEFAULT 0,
    saved_bytes INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    handle_created INTEGER NOT NULL DEFAULT 0,
    is_error INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_upstream ON calls(upstream);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    return con


class MetricsDB:
    """Thin wrapper. Instantiate once per process. `record()` is fire-and-forget."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_db_path()
        self._con: sqlite3.Connection | None = None
        try:
            self._con = _connect(self.path)
        except (OSError, sqlite3.Error) as e:
            log.debug("metrics disabled (open failed): %s", e)
            self._con = None

    @property
    def enabled(self) -> bool:
        return self._con is not None

    def record(
        self,
        *,
        upstream: str,
        tool: str,
        request_bytes: int = 0,
        raw_bytes: int = 0,
        response_bytes: int = 0,
        saved_bytes: int = 0,
        duration_ms: int = 0,
        handle_created: bool = False,
        is_error: bool = False,
        ts: float | None = None,
    ) -> None:
        if self._con is None:
            return
        try:
            self._con.execute(
                "INSERT INTO calls (ts, upstream, tool, request_bytes, raw_bytes, "
                "response_bytes, saved_bytes, duration_ms, handle_created, is_error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ts if ts is not None else time.time(),
                    upstream, tool,
                    int(request_bytes), int(raw_bytes), int(response_bytes),
                    int(saved_bytes), int(duration_ms),
                    1 if handle_created else 0,
                    1 if is_error else 0,
                ),
            )
        except sqlite3.Error as e:
            log.debug("metrics insert failed: %s", e)

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
            self._con = None


# ---------------------------------------------------------------------------
# Read-side helpers — used by the dashboard
# ---------------------------------------------------------------------------


@contextmanager
def _reader(path: Path) -> Iterator[sqlite3.Connection]:
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def clear_all(path: Path | None = None) -> int:
    """Delete every row in the calls table. Returns the number of rows removed."""
    p = path or default_db_path()
    if not p.exists():
        return 0
    con = sqlite3.connect(p)
    try:
        n = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        con.execute("DELETE FROM calls")
        con.commit()
        return int(n)
    finally:
        con.close()


# Rows with zero saved_bytes (jmunch.* handle ops, passthroughs below threshold,
# pure errors) are always hidden from the dashboard — they're noise for a
# savings-oriented view.
_ONLY_WITH_SAVINGS = "saved_bytes > 0"


def totals(path: Path | None = None) -> dict:
    """Cumulative totals across the whole DB."""
    p = path or default_db_path()
    if not p.exists():
        return _empty_totals()
    with _reader(p) as con:
        row = con.execute(
            "SELECT COUNT(*) AS n, "
            "       COALESCE(SUM(raw_bytes), 0) AS raw, "
            "       COALESCE(SUM(response_bytes), 0) AS sent, "
            "       COALESCE(SUM(saved_bytes), 0) AS saved, "
            "       COALESCE(SUM(handle_created), 0) AS handles, "
            "       COALESCE(SUM(is_error), 0) AS errors "
            f"FROM calls WHERE {_ONLY_WITH_SAVINGS}"
        ).fetchone()
    return {
        "calls": row["n"],
        "raw_bytes": row["raw"],
        "response_bytes": row["sent"],
        "saved_bytes": row["saved"],
        "handles_created": row["handles"],
        "errors": row["errors"],
        "tokens_saved": row["saved"] // 4,
    }


def _empty_totals() -> dict:
    return {"calls": 0, "raw_bytes": 0, "response_bytes": 0, "saved_bytes": 0,
            "handles_created": 0, "errors": 0, "tokens_saved": 0}


def per_upstream(path: Path | None = None) -> list[dict]:
    p = path or default_db_path()
    if not p.exists():
        return []
    with _reader(p) as con:
        rows = con.execute(
            "SELECT upstream, COUNT(*) AS n, "
            "       COALESCE(SUM(raw_bytes), 0) AS raw, "
            "       COALESCE(SUM(response_bytes), 0) AS sent, "
            "       COALESCE(SUM(saved_bytes), 0) AS saved, "
            "       COALESCE(SUM(duration_ms), 0) AS ms "
            f"FROM calls WHERE {_ONLY_WITH_SAVINGS} "
            "GROUP BY upstream ORDER BY saved DESC"
        ).fetchall()
    return [
        {
            "upstream": r["upstream"],
            "calls": r["n"],
            "raw_bytes": r["raw"],
            "response_bytes": r["sent"],
            "saved_bytes": r["saved"],
            "tokens_saved": r["saved"] // 4,
            "duration_ms": r["ms"],
        }
        for r in rows
    ]


def recent_calls(limit: int = 50, path: Path | None = None) -> list[dict]:
    p = path or default_db_path()
    if not p.exists():
        return []
    with _reader(p) as con:
        rows = con.execute(
            "SELECT ts, upstream, tool, raw_bytes, response_bytes, saved_bytes, "
            "       duration_ms, handle_created, is_error "
            f"FROM calls WHERE {_ONLY_WITH_SAVINGS} "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def series(bucket_seconds: int = 300, hours: int = 24, path: Path | None = None) -> list[dict]:
    """Time-series of savings, bucketed. Default: 5-min buckets over 24h."""
    p = path or default_db_path()
    if not p.exists():
        return []
    cutoff = time.time() - hours * 3600
    with _reader(p) as con:
        rows = con.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, "
            "       COUNT(*) AS n, "
            "       COALESCE(SUM(saved_bytes), 0) AS saved, "
            "       COALESCE(SUM(raw_bytes), 0) AS raw "
            f"FROM calls WHERE ts >= ? AND {_ONLY_WITH_SAVINGS} "
            "GROUP BY bucket ORDER BY bucket ASC",
            (bucket_seconds, bucket_seconds, cutoff),
        ).fetchall()
    return [
        {"ts": r["bucket"], "calls": r["n"],
         "saved_bytes": r["saved"], "raw_bytes": r["raw"],
         "tokens_saved": r["saved"] // 4}
        for r in rows
    ]
