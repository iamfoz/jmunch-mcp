"""Handle-ified tool_results sit in conversation history, which apps re-send on
every turn. LLM providers only serve a prompt-cache hit while every earlier
message is byte-identical to the previous request, so the same payload must
always handle-ify to exactly the same text: same handle ID (also across
gateway restarts) and no per-call values in the envelope."""
from __future__ import annotations

import json
from pathlib import Path

from jmunch_mcp.gateway.handleify import _content_hash, maybe_handleify
from jmunch_mcp.meta import SavingsTracker, envelope
from jmunch_mcp.registry import HandleRegistry


def _fat(n: int = 400) -> str:
    return json.dumps([{"id": i, "name": f"r-{i}", "desc": "x" * 100} for i in range(n)])


def _text(n: int = 400) -> str:
    return "\n".join(f"{i:4d}  line {i} of a source file being read by an agent" for i in range(n))


def _tracker(tmp_path: Path) -> SavingsTracker:
    return SavingsTracker(path=tmp_path / "_savings.json")


def test_same_payload_gives_identical_envelope_text(tmp_path):
    reg = HandleRegistry()
    tracker = _tracker(tmp_path)
    for payload in (_fat(), _text()):
        first = maybe_handleify(payload, registry=reg, tracker=tracker, threshold_tokens=100)
        second = maybe_handleify(payload, registry=reg, tracker=tracker, threshold_tokens=100)
        assert first is not None and second is not None
        assert first[0] == second[0]  # byte-identical, not just the same handle


def test_identical_across_restarts(tmp_path):
    """A fresh registry (a restarted gateway) must produce the same text too."""
    payload = _fat()
    before = maybe_handleify(payload, registry=HandleRegistry(), tracker=_tracker(tmp_path), threshold_tokens=100)
    after = maybe_handleify(payload, registry=HandleRegistry(), tracker=_tracker(tmp_path), threshold_tokens=100)
    assert before is not None and after is not None
    assert before == after
    assert before[1] == f"h_{_content_hash(payload)[:24]}"


def test_history_envelope_has_no_per_call_values(tmp_path):
    out = maybe_handleify(_fat(), registry=HandleRegistry(), tracker=_tracker(tmp_path), threshold_tokens=100)
    assert out is not None
    meta = json.loads(out[0])["_meta"]
    for key in ("total_tokens_saved", "total_cost_avoided", "timing_ms"):
        assert key not in meta
    assert meta["tokens_saved"] > 0


def test_savings_are_still_recorded(tmp_path):
    tracker = _tracker(tmp_path)
    reg = HandleRegistry()
    maybe_handleify(_fat(), registry=reg, tracker=tracker, threshold_tokens=100)
    after_one = tracker.total
    maybe_handleify(_fat(), registry=reg, tracker=tracker, threshold_tokens=100)
    assert after_one > 0
    assert tracker.total > after_one


def test_registering_the_same_content_again_replaces_the_handle(tmp_path):
    """After the hash index is lost (eviction), re-ingesting the same payload
    reuses the content-derived ID without double-counting its size."""
    reg = HandleRegistry()
    payload = _fat()
    first = maybe_handleify(payload, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    size = reg.total_bytes
    reg._by_hash.clear()  # simulate a lost index entry
    second = maybe_handleify(payload, registry=reg, tracker=_tracker(tmp_path), threshold_tokens=100)
    assert first[1] == second[1]
    assert len(reg) == 1
    assert reg.total_bytes == size


def test_stable_envelope_default_is_unchanged(tmp_path):
    """Envelopes for live responses keep their running totals and timing."""
    meta = envelope(result={}, raw_bytes=1000, response_bytes=10,
                    tracker=_tracker(tmp_path), timing_ms=1.5)["_meta"]
    assert {"total_tokens_saved", "total_cost_avoided", "timing_ms"} <= meta.keys()
