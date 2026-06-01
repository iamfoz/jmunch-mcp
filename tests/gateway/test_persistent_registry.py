"""Persistent handle registry survives a restart and expires by TTL."""
from __future__ import annotations

import time

from jmunch_mcp.backends.tabular import TabularBackend
from jmunch_mcp.backends.text import TextBackend
from jmunch_mcp.persistent_registry import PersistentHandleRegistry


def _rows(n: int = 20):
    return [{"id": i, "name": f"r-{i}"} for i in range(n)]


def test_register_and_in_memory_get(tmp_path):
    reg = PersistentHandleRegistry(store_path=tmp_path / "h.db", ttl_seconds=60)
    rows = _rows()
    be = TabularBackend(rows)
    h = reg.register(be, be.size_bytes, "tabular", source=rows)
    assert h.id.startswith("h_")
    # Hot path (same instance) returns from memory.
    got = reg.get(h.id)
    assert got is not None and got is h
    reg.close_db()


def test_get_after_restart_rehydrates(tmp_path):
    db = tmp_path / "h.db"
    rows = _rows()
    reg1 = PersistentHandleRegistry(store_path=db, ttl_seconds=60)
    be = TabularBackend(rows)
    h = reg1.register(be, be.size_bytes, "tabular", source=rows)
    handle_id = h.id
    reg1.close_db()

    # Simulate a restart: fresh registry, same DB path.
    reg2 = PersistentHandleRegistry(store_path=db, ttl_seconds=60)
    got = reg2.get(handle_id)
    assert got is not None
    assert got.kind == "tabular"
    # Backend was rebuilt and answers peek.
    peeked = got.backend.peek(3)
    assert len(peeked) == 3
    assert peeked[0] == {"id": 0, "name": "r-0"}
    reg2.close_db()


def test_text_backend_rehydrates(tmp_path):
    db = tmp_path / "h.db"
    text = "line one\n" * 500
    reg1 = PersistentHandleRegistry(store_path=db, ttl_seconds=60)
    be = TextBackend(text)
    h = reg1.register(be, be.size_bytes, "text", source=text)
    hid = h.id
    reg1.close_db()
    reg2 = PersistentHandleRegistry(store_path=db, ttl_seconds=60)
    got = reg2.get(hid)
    assert got is not None and got.kind == "text"
    reg2.close_db()


def test_expired_handle_swept(tmp_path):
    reg = PersistentHandleRegistry(store_path=tmp_path / "h.db", ttl_seconds=1)
    rows = _rows()
    be = TabularBackend(rows)
    h = reg.register(be, be.size_bytes, "tabular", source=rows)
    # Simulate time passing by sweeping with a future "now".
    removed = reg.sweep_expired(now=time.time() + 3600)
    assert removed == 1
    # After sweep, rehydration from a fresh registry misses.
    reg.close_db()
    reg2 = PersistentHandleRegistry(store_path=tmp_path / "h.db", ttl_seconds=1)
    assert reg2.get(h.id) is None
    reg2.close_db()


def test_register_without_source_skips_persist(tmp_path):
    """Backward-compat: callers that don't pass `source` get in-memory only."""
    db = tmp_path / "h.db"
    reg1 = PersistentHandleRegistry(store_path=db, ttl_seconds=60)
    be = TabularBackend(_rows())
    h = reg1.register(be, be.size_bytes, "tabular")  # no source
    hid = h.id
    reg1.close_db()
    reg2 = PersistentHandleRegistry(store_path=db, ttl_seconds=60)
    assert reg2.get(hid) is None
    reg2.close_db()


def test_register_accepts_content_hash_kwarg(tmp_path):
    """The persistent subclass must forward `content_hash` to the in-memory
    base so the gateway's dedup path works against the on-disk registry."""
    reg = PersistentHandleRegistry(store_path=tmp_path / "h.db", ttl_seconds=60)
    rows = _rows()
    be = TabularBackend(rows)
    h = reg.register(be, be.size_bytes, "tabular", source=rows, content_hash="abc123")
    assert h.content_hash == "abc123"
    assert reg.find_by_hash("abc123") is h
    reg.close_db()
