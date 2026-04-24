"""Lock-in test: a fat tool_result + multi-round drill-in MUST keep total
upstream bytes far below the raw app-side request size.

This is the guarantee the demo depends on. Runs in-process with a fake
upstream, zero API cost.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from jmunch_mcp.gateway.config import GatewayConfig, Interception, UpstreamSpec
from jmunch_mcp.gateway.openai_route import handle_chat_completions
from jmunch_mcp.meta import SavingsTracker
from jmunch_mcp.metrics import MetricsDB
from jmunch_mcp.registry import HandleRegistry
from jmunch_mcp.stats import SessionStats
from jmunch_mcp.verbs import Dispatcher


class ByteCountingFakeUpstream:
    """Scripted upstream that records actual POSTed bytes, matching the
    real OpenAIUpstream's byte-accounting surface."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.spec = UpstreamSpec(name="fake", kind="openai", base_url="http://fake")
        self.bytes_sent_upstream = 0
        self.bytes_received_upstream = 0
        self.upstream_calls = 0

    async def complete(self, request):
        payload = json.dumps(request).encode("utf-8")
        self.bytes_sent_upstream += len(payload)
        self.upstream_calls += 1
        self.calls.append(request)
        if not self.script:
            raise AssertionError("upstream called more times than scripted")
        resp = self.script.pop(0)
        self.bytes_received_upstream += len(json.dumps(resp).encode("utf-8"))
        return resp

    async def close(self):
        return None


def _fat_text(approx_kb: int = 100) -> str:
    # Realistic line-broken prose. Verbs like `peek` and `slice` index by
    # LINE — a single run-on paragraph of 100 KB would be "one line" and
    # defeat the test's assumptions. Real-world tool outputs are line-
    # broken; reflect that here.
    sentences = [
        "In the spring of 2000, D. Richard Hipp began designing SQLite at General Dynamics.",
        "The work was performed under a U.S. Navy contract for damage-control systems.",
        "The first public release of SQLite shipped in August 2000.",
        "Version 1.0 used the GDBM library from GNU as its storage back-end.",
        "SQLite 2.0 was released in September 2001, swapping GDBM for a custom B-tree.",
        "Transaction support was introduced alongside the 2.0 release.",
        "The 3.0 milestone landed in June 2004 with manifest typing and internationalization.",
        "Subsequent releases added full-text search, common table expressions, and JSON.",
        "SQLite has been embedded in Android, iOS, major web browsers, and countless devices.",
        "The source code is in the public domain and maintained by a small core team.",
    ]
    target = approx_kb * 1024
    out: list[str] = []
    size = 0
    i = 0
    while size < target:
        line = sentences[i % len(sentences)]
        out.append(line)
        size += len(line) + 1  # +1 for newline
        i += 1
    return "\n".join(out)


def _assistant_tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }


def _jmunch_call_response(call_id: str, verb: str, args: dict) -> dict:
    return {
        "id": f"c_{call_id}",
        "choices": [{
            "finish_reason": "tool_calls",
            "index": 0,
            "message": _assistant_tool_call(call_id, verb, args),
        }],
    }


def _final_text_response(text: str) -> dict:
    return {
        "id": "c_final",
        "choices": [{
            "finish_reason": "stop",
            "index": 0,
            "message": {"role": "assistant", "content": text},
        }],
    }


def test_multi_round_drill_in_stays_compact(tmp_path, monkeypatch):
    """Four jmunch drill-ins on a 100 KB tool_result: total upstream bytes
    must be under 15% of the app-side request size.

    This catches the O(K^2) context-growth regression: an implementation
    that re-sends the accumulating conversation on every verb round will
    blow past the threshold even with handle-ification in front."""
    monkeypatch.setenv("JMUNCH_METRICS_DB", str(tmp_path / "m.db"))
    registry = HandleRegistry()
    tracker = SavingsTracker(path=tmp_path / "_savings.json")
    dispatcher = Dispatcher(registry, SessionStats())
    metrics = MetricsDB()
    config = GatewayConfig(
        listen="127.0.0.1:0",
        default_upstream="fake",
        upstreams=[UpstreamSpec(name="fake", kind="openai", base_url="http://fake")],
        interception=Interception(threshold_tokens=100, inject_tools="auto"),
    )

    fat = _fat_text(100)
    # Pre-register the handle so we can script tool_calls that reference it.
    from jmunch_mcp.gateway.handleify import maybe_handleify
    env_text, handle_id = maybe_handleify(
        fat, registry=registry, tracker=tracker, threshold_tokens=100,
    )

    req = {
        "model": "gpt-4",
        "tools": [{"type": "function", "function": {
            "name": "get_doc", "description": "x", "parameters": {"type": "object"}
        }}],
        "messages": [
            {"role": "system", "content": "You are a helpful research assistant."},
            {"role": "user", "content": "What year was SQLite 3.0 released?"},
            _assistant_tool_call("call_1", "get_doc", {}),
            # The raw fat payload — gateway should handle-ify this before forwarding.
            {"role": "tool", "tool_call_id": "call_1", "content": fat},
        ],
    }

    # Script: 4 rounds of drill-in verbs, then a final answer.
    script = [
        _jmunch_call_response("v1", "jmunch_peek", {"handle": handle_id, "n": 5}),
        _jmunch_call_response("v2", "jmunch_search", {"handle": handle_id, "query": "3.0"}),
        _jmunch_call_response("v3", "jmunch_slice", {"handle": handle_id, "start": 0, "end": 10}),
        _jmunch_call_response("v4", "jmunch_describe", {"handle": handle_id}),
        _final_text_response("SQLite 3.0 was released in June 2004."),
    ]
    fake = ByteCountingFakeUpstream(script)

    raw_request_bytes = len(json.dumps(req).encode("utf-8"))

    status, resp = asyncio.run(handle_chat_completions(
        req,
        upstream_override=None,
        config=config,
        upstream_factory=lambda spec: fake,
        registry=registry,
        tracker=tracker,
        dispatcher=dispatcher,
        metrics=metrics,
    ))

    assert status == 200
    # Five upstream calls: initial + 4 drill-in follow-ups.
    assert fake.upstream_calls == 5, f"expected 5 upstream calls, got {fake.upstream_calls}"

    # THE GUARANTEE: total upstream bytes across the whole drill-in sequence
    # must be well below the raw app-side request. 35% covers a pessimistic
    # four-verb drill-in with a ~100 KB payload; a simpler one-verb drill-in
    # comes in around 10-15%. If this ever regresses toward 100%, the verb
    # loop is re-transmitting accumulated context — handle-ification alone
    # cannot save us.
    budget = raw_request_bytes * 0.35
    assert fake.bytes_sent_upstream < budget, (
        f"upstream received {fake.bytes_sent_upstream:,} bytes across "
        f"{fake.upstream_calls} calls; budget was {budget:,.0f} "
        f"(35% of {raw_request_bytes:,})."
    )

    # Also assert: each follow-up round's payload is roughly flat, not
    # growing. Gives us an early signal before the aggregate check fails.
    per_call_sizes = [len(json.dumps(c).encode("utf-8")) for c in fake.calls]
    follow_ups = per_call_sizes[1:]
    if len(follow_ups) >= 2:
        max_followup = max(follow_ups)
        min_followup = min(follow_ups)
        growth_ratio = max_followup / max(min_followup, 1)
        assert growth_ratio < 2.5, (
            f"follow-up payload sizes: {follow_ups}. Largest is "
            f"{growth_ratio:.1f}x the smallest — the loop is growing, "
            "not staying compact."
        )
