"""jMRI _meta envelope + savings persistence.

Every jmunch-mcp response (result or error) MUST be wrapped via `envelope()`.
Token accounting follows the jMRI spec: bytes/4, no tokenizer dependency.
Cumulative `total_tokens_saved` is persisted to ~/.jmunch/_savings.json.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__

BYTES_PER_TOKEN = 4  # jMRI spec: conservative, zero-overhead approximation

RETRIEVAL_ENGINE = "jmunch"
RETRIEVAL_VERSION = "1.0"
POWERED_BY = "jmunch-mcp by jgravelle · https://github.com/jgravelle/jmunch-mcp"

# Per-model USD per 1M input tokens. Same spirit as the jCodeMunch/jDocMunch
# cost table; keep in sync as pricing shifts.
_MODEL_PRICES_PER_1M: dict[str, float] = {
    "claude_opus": 15.00,
    "claude_sonnet": 3.00,
    "gpt5_latest": 10.00,
}


def estimate_tokens(n_bytes: int) -> int:
    return max(0, n_bytes // BYTES_PER_TOKEN)


def estimate_savings(raw_bytes: int, response_bytes: int) -> int:
    return max(0, (raw_bytes - response_bytes) // BYTES_PER_TOKEN)


def cost_avoided(tokens: int) -> dict[str, float]:
    return {k: round(tokens * price / 1_000_000, 4) for k, price in _MODEL_PRICES_PER_1M.items()}


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    """Best-effort cross-process exclusive lock.

    Multiple proxy/gateway processes share one `_savings.json`; without a
    cross-process lock their read-modify-write cycles interleave and one
    process's increment is silently lost. This serialises them via an OS
    advisory lock on a sibling `.lock` file. Degrades to a no-op (current
    behaviour) if OS file locking is unavailable.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
    except OSError:
        yield
        return
    locked = False
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except (OSError, ImportError):
            locked = False
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class SavingsTracker:
    """Persists cumulative tokens saved across process restarts.

    Safe for concurrent processes: each `record()` re-reads the on-disk
    totals under a cross-process file lock, so two proxies sharing one
    `_savings.json` cannot lose each other's increments.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / ".jmunch" / "_savings.json")
        self._lock_path = self.path.with_suffix(".lock")
        self._lock = threading.Lock()
        self._total_tokens_saved = 0
        self._total_cost_avoided: dict[str, float] = {k: 0.0 for k in _MODEL_PRICES_PER_1M}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._total_tokens_saved = int(data.get("total_tokens_saved", 0))
            stored_cost = data.get("total_cost_avoided", {})
            for k in self._total_cost_avoided:
                self._total_cost_avoided[k] = float(stored_cost.get(k, 0.0))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "total_tokens_saved": self._total_tokens_saved,
                    "total_cost_avoided": self._total_cost_avoided,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def record(self, tokens_saved: int) -> tuple[int, dict[str, float]]:
        with self._lock, _file_lock(self._lock_path):
            # Re-read the authoritative on-disk totals inside the lock —
            # another process may have advanced them since we last loaded.
            # Without this re-read the read-modify-write races and one
            # process's increment is silently overwritten.
            self._load()
            self._total_tokens_saved += tokens_saved
            delta_cost = cost_avoided(tokens_saved)
            for k, v in delta_cost.items():
                self._total_cost_avoided[k] = round(self._total_cost_avoided[k] + v, 4)
            self._persist()
            return self._total_tokens_saved, dict(self._total_cost_avoided)

    @property
    def total(self) -> int:
        return self._total_tokens_saved


def _meta_block(
    *,
    tokens_saved: int,
    response_bytes: int,
    raw_bytes: int,
    total_saved: int,
    total_cost: dict[str, float],
    timing_ms: float | None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "tokens_saved": tokens_saved,
        "total_tokens_saved": total_saved,
        "response_tokens": estimate_tokens(response_bytes),
        "naive_tokens": estimate_tokens(raw_bytes),
        "cost_avoided": cost_avoided(tokens_saved),
        "total_cost_avoided": total_cost,
        "retrieval_engine": RETRIEVAL_ENGINE,
        "retrieval_version": RETRIEVAL_VERSION,
        "jmunch_version": __version__,
        "powered_by": POWERED_BY,
    }
    if timing_ms is not None:
        meta["timing_ms"] = round(timing_ms, 2)
    return meta


def _wrap(result: Any, error: dict[str, Any] | None, meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"_meta": meta}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def envelope(
    *,
    result: Any = None,
    error: dict[str, Any] | None = None,
    raw_bytes: int,
    response_bytes: int | None = None,
    tracker: SavingsTracker,
    timing_ms: float | None = None,
) -> dict[str, Any]:
    """Wrap a result or error in the jMRI response envelope.

    `raw_bytes` is the original upstream payload size. `response_bytes` is the
    compact response being emitted; pass it when the size is already known
    (passthrough / error envelopes).

    Leave `response_bytes` as None for handle-ification, where the compact
    response *is* this envelope and its size isn't known until it exists. In
    that case the envelope measures its own serialized length and records the
    true savings to the tracker exactly once — passing `response_bytes=0`
    here would credit the tracker with `raw_bytes` of savings it never made.
    """
    if response_bytes is None:
        # Draft with worst-case (response_bytes=0) numbers to size the
        # structure, then record the true savings against the measured size.
        draft = _wrap(result, error, _meta_block(
            tokens_saved=estimate_savings(raw_bytes, 0),
            response_bytes=0,
            raw_bytes=raw_bytes,
            total_saved=tracker.total,
            total_cost={k: 0.0 for k in _MODEL_PRICES_PER_1M},
            timing_ms=timing_ms,
        ))
        measured = len(json.dumps(draft, default=str))
        tokens_saved = estimate_savings(raw_bytes, measured)
        total_saved, total_cost = tracker.record(tokens_saved)
        return _wrap(result, error, _meta_block(
            tokens_saved=tokens_saved,
            response_bytes=measured,
            raw_bytes=raw_bytes,
            total_saved=total_saved,
            total_cost=total_cost,
            timing_ms=timing_ms,
        ))

    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved, total_cost = tracker.record(tokens_saved)
    return _wrap(result, error, _meta_block(
        tokens_saved=tokens_saved,
        response_bytes=response_bytes,
        raw_bytes=raw_bytes,
        total_saved=total_saved,
        total_cost=total_cost,
        timing_ms=timing_ms,
    ))


def timer_ms(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000
