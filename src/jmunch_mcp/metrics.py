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


_SCHEMA_TABLE = """
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
    is_error INTEGER NOT NULL DEFAULT 0,
    surface TEXT NOT NULL DEFAULT 'mcp',
    tokens_saved_exact INTEGER NOT NULL DEFAULT 0,
    upstream_bytes_sent INTEGER NOT NULL DEFAULT 0,
    upstream_bytes_received INTEGER NOT NULL DEFAULT 0,
    upstream_calls INTEGER NOT NULL DEFAULT 0
);
"""

_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_upstream ON calls(upstream);
CREATE INDEX IF NOT EXISTS idx_calls_surface ON calls(surface);
"""


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns added post-launch. SQLite pre-3.35 has no IF NOT EXISTS
    for columns, so we sniff via PRAGMA table_info."""
    cols = {row[1] for row in con.execute("PRAGMA table_info(calls)")}
    if "surface" not in cols:
        con.execute("ALTER TABLE calls ADD COLUMN surface TEXT NOT NULL DEFAULT 'mcp'")
    if "tokens_saved_exact" not in cols:
        con.execute("ALTER TABLE calls ADD COLUMN tokens_saved_exact INTEGER NOT NULL DEFAULT 0")
    if "upstream_bytes_sent" not in cols:
        con.execute("ALTER TABLE calls ADD COLUMN upstream_bytes_sent INTEGER NOT NULL DEFAULT 0")
    if "upstream_bytes_received" not in cols:
        con.execute("ALTER TABLE calls ADD COLUMN upstream_bytes_received INTEGER NOT NULL DEFAULT 0")
    if "upstream_calls" not in cols:
        con.execute("ALTER TABLE calls ADD COLUMN upstream_calls INTEGER NOT NULL DEFAULT 0")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA_TABLE)
    _migrate(con)
    con.executescript(_SCHEMA_INDEXES)
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
        surface: str = "mcp",
        tokens_saved_exact: int = 0,
        upstream_bytes_sent: int = 0,
        upstream_bytes_received: int = 0,
        upstream_calls: int = 0,
    ) -> None:
        if self._con is None:
            return
        try:
            self._con.execute(
                "INSERT INTO calls (ts, upstream, tool, request_bytes, raw_bytes, "
                "response_bytes, saved_bytes, duration_ms, handle_created, is_error, "
                "surface, tokens_saved_exact, upstream_bytes_sent, "
                "upstream_bytes_received, upstream_calls) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts if ts is not None else time.time(),
                    upstream, tool,
                    int(request_bytes), int(raw_bytes), int(response_bytes),
                    int(saved_bytes), int(duration_ms),
                    1 if handle_created else 0,
                    1 if is_error else 0,
                    surface,
                    int(tokens_saved_exact),
                    int(upstream_bytes_sent),
                    int(upstream_bytes_received),
                    int(upstream_calls),
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


def _surface_clause(surface: str | None) -> tuple[str, tuple]:
    """Build SQL snippet + params for filtering by surface. `None` or 'all' → no filter."""
    if surface and surface != "all":
        return "AND surface = ?", (surface,)
    return "", ()


def totals(path: Path | None = None, *, surface: str | None = None,
           include_zero_savings: bool = False) -> dict:
    """Cumulative totals across the whole DB.

    By default the dashboard-oriented `saved_bytes > 0` filter is applied so
    zero-savings rows (passthroughs, handle ops, errors) don't skew the
    savings view. Pass `include_zero_savings=True` for baseline measurements
    where you need the raw traffic total regardless of interception outcome.
    """
    p = path or default_db_path()
    if not p.exists():
        return _empty_totals()
    clause, params = _surface_clause(surface)
    where = "1=1" if include_zero_savings else _ONLY_WITH_SAVINGS
    with _reader(p) as con:
        row = con.execute(
            "SELECT COUNT(*) AS n, "
            "       COALESCE(SUM(raw_bytes), 0) AS raw, "
            "       COALESCE(SUM(response_bytes), 0) AS sent, "
            "       COALESCE(SUM(saved_bytes), 0) AS saved, "
            "       COALESCE(SUM(tokens_saved_exact), 0) AS saved_exact, "
            "       COALESCE(SUM(handle_created), 0) AS handles, "
            "       COALESCE(SUM(is_error), 0) AS errors, "
            "       COALESCE(SUM(upstream_bytes_sent), 0) AS up_sent, "
            "       COALESCE(SUM(upstream_bytes_received), 0) AS up_recv, "
            "       COALESCE(SUM(upstream_calls), 0) AS up_calls "
            f"FROM calls WHERE {where} {clause}",
            params,
        ).fetchone()
    return {
        "calls": row["n"],
        "raw_bytes": row["raw"],
        "response_bytes": row["sent"],
        "saved_bytes": row["saved"],
        "handles_created": row["handles"],
        "errors": row["errors"],
        "tokens_saved": row["saved"] // 4,
        "tokens_saved_exact": row["saved_exact"],
        "upstream_bytes_sent": row["up_sent"],
        "upstream_bytes_received": row["up_recv"],
        "upstream_calls": row["up_calls"],
    }


def _empty_totals() -> dict:
    return {"calls": 0, "raw_bytes": 0, "response_bytes": 0, "saved_bytes": 0,
            "handles_created": 0, "errors": 0, "tokens_saved": 0,
            "tokens_saved_exact": 0,
            "upstream_bytes_sent": 0, "upstream_bytes_received": 0, "upstream_calls": 0}


def per_upstream(path: Path | None = None, *, surface: str | None = None) -> list[dict]:
    p = path or default_db_path()
    if not p.exists():
        return []
    clause, params = _surface_clause(surface)
    with _reader(p) as con:
        rows = con.execute(
            "SELECT upstream, surface, COUNT(*) AS n, "
            "       COALESCE(SUM(raw_bytes), 0) AS raw, "
            "       COALESCE(SUM(response_bytes), 0) AS sent, "
            "       COALESCE(SUM(saved_bytes), 0) AS saved, "
            "       COALESCE(SUM(tokens_saved_exact), 0) AS saved_exact, "
            "       COALESCE(SUM(duration_ms), 0) AS ms "
            f"FROM calls WHERE {_ONLY_WITH_SAVINGS} {clause} "
            "GROUP BY upstream, surface ORDER BY saved DESC",
            params,
        ).fetchall()
    return [
        {
            "upstream": r["upstream"],
            "surface": r["surface"],
            "calls": r["n"],
            "raw_bytes": r["raw"],
            "response_bytes": r["sent"],
            "saved_bytes": r["saved"],
            "tokens_saved": r["saved"] // 4,
            "tokens_saved_exact": r["saved_exact"],
            "duration_ms": r["ms"],
        }
        for r in rows
    ]


def recent_calls(limit: int = 50, path: Path | None = None, *, surface: str | None = None) -> list[dict]:
    p = path or default_db_path()
    if not p.exists():
        return []
    clause, params = _surface_clause(surface)
    with _reader(p) as con:
        rows = con.execute(
            "SELECT ts, upstream, surface, tool, raw_bytes, response_bytes, "
            "       saved_bytes, tokens_saved_exact, duration_ms, handle_created, is_error "
            f"FROM calls WHERE {_ONLY_WITH_SAVINGS} {clause} "
            "ORDER BY id DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def series(bucket_seconds: int = 300, hours: int = 24,
           path: Path | None = None, *, surface: str | None = None) -> list[dict]:
    """Time-series of savings, bucketed. Default: 5-min buckets over 24h."""
    p = path or default_db_path()
    if not p.exists():
        return []
    cutoff = time.time() - hours * 3600
    clause, params = _surface_clause(surface)
    with _reader(p) as con:
        rows = con.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, "
            "       COUNT(*) AS n, "
            "       COALESCE(SUM(saved_bytes), 0) AS saved, "
            "       COALESCE(SUM(raw_bytes), 0) AS raw "
            f"FROM calls WHERE ts >= ? AND {_ONLY_WITH_SAVINGS} {clause} "
            "GROUP BY bucket ORDER BY bucket ASC",
            (bucket_seconds, bucket_seconds, cutoff, *params),
        ).fetchall()
    return [
        {"ts": r["bucket"], "calls": r["n"],
         "saved_bytes": r["saved"], "raw_bytes": r["raw"],
         "tokens_saved": r["saved"] // 4}
        for r in rows
    ]
