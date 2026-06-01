"""Handle registry: opaque IDs → backend-specific stores with LRU eviction.

Handles are session-scoped by design (PRD §4 non-goal — explicit deviation
from jMRI's persistent-ID rule). Eviction returns a structured
HANDLE_EXPIRED error on query; agents re-fetch from upstream.

The registry is agnostic to backend details — it just holds a `Handle` object
whose backend knows how to answer verbs. Approximate byte size is tracked for
the size cap; backends report their own size on ingest.
"""
from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends.base import Backend


@dataclass
class Handle:
    id: str
    backend: "Backend"
    size_bytes: int
    kind: str  # sniffer.Kind value
    content_hash: str | None = None   # SHA-256 of the original payload, if known


DEFAULT_MAX_HANDLES = 1000
DEFAULT_MAX_BYTES = 500 * 1024 * 1024  # 500 MB


class HandleRegistry:
    def __init__(
        self,
        *,
        max_handles: int = DEFAULT_MAX_HANDLES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._store: OrderedDict[str, Handle] = OrderedDict()
        self._by_hash: dict[str, str] = {}   # content_hash → handle_id (for dedup)
        self._total_bytes = 0
        self._max_handles = max_handles
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def register(
        self,
        backend: "Backend",
        size_bytes: int,
        kind: str,
        *,
        source: object | None = None,   # ignored by the in-memory registry; the
                                         # persistent subclass uses it to write
                                         # the source payload to disk.
        content_hash: str | None = None,  # if supplied, used for dedup lookups
    ) -> Handle:
        handle_id = f"h_{secrets.token_urlsafe(9)}"
        h = Handle(id=handle_id, backend=backend, size_bytes=size_bytes,
                   kind=kind, content_hash=content_hash)
        with self._lock:
            self._store[handle_id] = h
            if content_hash:
                self._by_hash[content_hash] = handle_id
            self._total_bytes += size_bytes
            self._evict_if_needed()
        return h

    def find_by_hash(self, content_hash: str) -> Handle | None:
        """Return the existing handle for this content hash, or None.

        Used for content-addressed dedup — identical incoming payloads share
        one handle (and one backend) within the process. Bumps recency so a
        hot handle isn't evicted by the LRU."""
        if not content_hash:
            return None
        with self._lock:
            handle_id = self._by_hash.get(content_hash)
            if handle_id is None:
                return None
            h = self._store.get(handle_id)
            if h is None:
                # Stale index entry — drop it.
                self._by_hash.pop(content_hash, None)
                return None
            self._store.move_to_end(handle_id)
            return h

    def get(self, handle_id: str) -> Handle | None:
        with self._lock:
            h = self._store.get(handle_id)
            if h is not None:
                self._store.move_to_end(handle_id)
            return h

    def drop(self, handle_id: str) -> bool:
        with self._lock:
            h = self._store.pop(handle_id, None)
            if h is None:
                return False
            if h.content_hash:
                self._by_hash.pop(h.content_hash, None)
            self._total_bytes -= h.size_bytes
            try:
                h.backend.close()
            except Exception:
                pass
            return True

    def list(self) -> list[Handle]:
        with self._lock:
            return list(self._store.values())

    def _evict_if_needed(self) -> None:
        while self._store and (
            len(self._store) > self._max_handles or self._total_bytes > self._max_bytes
        ):
            oldest_id, oldest = self._store.popitem(last=False)
            if oldest.content_hash:
                self._by_hash.pop(oldest.content_hash, None)
            self._total_bytes -= oldest.size_bytes
            try:
                oldest.backend.close()
            except Exception:
                pass

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def __len__(self) -> int:
        return len(self._store)
