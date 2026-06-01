"""Content-addressed handle dedup: identical incoming payloads share one
handle within a registry. Lets multiple agents / sessions through the
same gateway re-use a single backend instead of redundantly ingesting
the same bytes."""
from __future__ import annotations

import json
from pathlib import Path

from jmunch_mcp.gateway.handleify import _content_hash, maybe_handleify
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.registry import HandleRegistry


def _fat(n: int = 400) -> str:
    """Comfortably-over-threshold JSON payload (~46 KB at n=400)."""
    return json.dumps([{"id": i, "name": f"r-{i}", "desc": "x" * 100} for i in range(n)])


def _tracker(tmp_path: Path) -> SavingsTracker:
    return SavingsTracker(path=tmp_path / "_savings.json")


def test_identical_content_returns_same_handle(tmp_path):
    reg = HandleRegistry()
    big = _fat()
    out1 = maybe_handleify(big, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    out2 = maybe_handleify(big, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    assert out1 is not None and out2 is not None
    env1, h1 = out1
    env2, h2 = out2
    assert h1 == h2                # same handle id
    assert len(reg) == 1           # one backend, not two
    # envelopes share the handle id but each round-trips through `envelope()`
    # so per-request _meta (timing_ms etc) is fresh on every call
    e1 = json.loads(env1); e2 = json.loads(env2)
    assert e1["result"]["handle"] == e2["result"]["handle"] == h1


def test_different_content_gets_different_handle(tmp_path):
    reg = HandleRegistry()
    a = maybe_handleify(_fat(400), registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    b = maybe_handleify(_fat(500), registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    assert a is not None and b is not None
    assert a[1] != b[1]
    assert len(reg) == 2


def test_dedup_survives_unrelated_handles_in_between(tmp_path):
    reg = HandleRegistry()
    big = _fat()
    a = maybe_handleify(big, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    # add an unrelated handle
    maybe_handleify(_fat(800), registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    # the same content comes back later — still dedups
    c = maybe_handleify(big, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    assert a is not None and c is not None
    assert a[1] == c[1]
    assert len(reg) == 2


def test_dropping_a_handle_clears_its_hash_entry(tmp_path):
    reg = HandleRegistry()
    big = _fat()
    out = maybe_handleify(big, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    assert out is not None
    _, h_id = out
    digest = _content_hash(big)
    assert reg.find_by_hash(digest) is not None
    reg.drop(h_id)
    # after drop, both the registry slot and the hash index are gone
    assert reg.find_by_hash(digest) is None
    assert len(reg) == 0
    # …and a fresh handle-ification creates a new handle (not the dropped one)
    out2 = maybe_handleify(big, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    assert out2 is not None and out2[1] != h_id


def test_eviction_clears_hash_index(tmp_path):
    reg = HandleRegistry(max_handles=2)
    a = maybe_handleify(_fat(400), registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    b = maybe_handleify(_fat(500), registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    # Insert a third; LRU evicts the first.
    c = maybe_handleify(_fat(600), registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    assert a is not None and b is not None and c is not None
    assert len(reg) == 2
    # The evicted handle's hash MUST be cleared, or a future identical
    # request would resolve to a dropped handle.
    assert reg.find_by_hash(_content_hash(_fat(400))) is None


def test_below_threshold_payload_does_not_register_a_hash(tmp_path):
    reg = HandleRegistry()
    small = json.dumps({"small": "payload"})  # well under threshold
    out = maybe_handleify(small, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=10_000)
    assert out is None
    assert len(reg) == 0
    assert reg.find_by_hash(_content_hash(small)) is None
